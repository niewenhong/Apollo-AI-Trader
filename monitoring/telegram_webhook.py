"""
monitoring/telegram_webhook.py - Telegram 命令轮询处理器
独立模块，不依赖 Flask，通过后台线程拉取消息并调用 RemoteController
"""
import threading
import time
from typing import Optional

import requests

from .telegram_notifier import TelegramNotifier


class TelegramCommandListener:
    def __init__(self, token: str, chat_id: str, controller, poll_interval: float = 3.0):
        self.token = token
        self.chat_id = str(chat_id)
        self.controller = controller
        self.poll_interval = poll_interval
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            print("[TelegramWebhook] 已在运行")
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print("[TelegramWebhook] 命令轮询已启动")

    def stop(self):
        self._running = False
        print("[TelegramWebhook] 命令轮询已停止")

    def _poll_loop(self):
        while self._running:
            try:
                url = f"{self.base_url}/getUpdates"
                params = {
                    "offset": self._offset,
                    "timeout": 10,
                    "allowed_updates": ["message"]
                }
                resp = requests.get(url, params=params, timeout=15)
                data = resp.json()
                if not data.get("ok"):
                    print(f"[TelegramWebhook] getUpdates 错误: {data}")
                    time.sleep(self.poll_interval)
                    continue

                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat = msg.get("chat", {})
                    user_id = chat.get("id")
                    if str(user_id) != self.chat_id:
                        continue

                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    print(f"[TelegramWebhook] 收到消息: {text}")

                    if text.startswith("/"):
                        parts = text.split(maxsplit=1)
                        command = parts[0][1:]
                        args = parts[1] if len(parts) > 1 else ""
                        try:
                            reply = self.controller.handle_command(command, args)
                        except Exception as e:
                            reply = f"命令执行出错: {e}"
                    else:
                        reply = "未知消息。输入 /help 查看帮助。"

                    self._send_reply(reply)

            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                print(f"[TelegramWebhook] 轮询异常: {e}")
                time.sleep(self.poll_interval)

    def _send_reply(self, text: str):
        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            print(f"[TelegramWebhook] 已回复: {text[:80]}")
        except Exception as e:
            print(f"[TelegramWebhook] 回复失败: {e}")