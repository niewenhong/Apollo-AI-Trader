"""
monitoring/telegram_webhook.py — Telegram 命令轮询监听器 v2.7.0
功能：通过长轮询接收用户命令并转发给 RemoteController
版本：v2.7.0
变更：2026-07-26 启动时自动清除历史消息缓存，防止旧命令被执行
"""

import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger("TelegramWebhook")


class TelegramCommandListener:
    def __init__(self, token: str, chat_id: str, controller,
                 poll_interval: float = 3.0,
                 proxy: Optional[str] = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.controller = controller
        self.poll_interval = poll_interval
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0  # 用于标记已处理的最新消息ID

    def start(self):
        """启动后台轮询线程"""
        if self._running:
            logger.warning("[TelegramWebhook] 已在运行")
            return
        self._running = True
        self._clear_pending_updates()  # ★ 启动时清除历史缓存
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="TelegramPoll")
        self._thread.start()
        logger.info("[TelegramWebhook] 命令轮询已启动")

    def stop(self):
        """停止轮询"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("[TelegramWebhook] 已停止")

    def _clear_pending_updates(self):
        """清除所有待处理的旧消息，防止启动时执行历史命令"""
        try:
            url = f"{self.base_url}/getUpdates"
            resp = self.session.post(url, json={
                "offset": -1,       # 获取最新的消息ID
                "timeout": 1,
                "allowed_updates": ["message"]
            }, timeout=5)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                # 取最新消息的 update_id 作为 offset，跳过所有历史
                max_update_id = max(u["update_id"] for u in data["result"])
                self._offset = max_update_id + 1
                logger.info(f"[TelegramWebhook] 已清除历史缓存，跳过 {len(data['result'])} 条旧消息")
            else:
                self._offset = 0
        except Exception as e:
            logger.warning(f"[TelegramWebhook] 清除缓存失败: {e}")
            self._offset = 0

    def _poll_loop(self):
        """轮询循环"""
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f"[TelegramWebhook] 轮询异常: {e}")
            time.sleep(self.poll_interval)

    def _poll_once(self):
        """单次轮询"""
        url = f"{self.base_url}/getUpdates"
        payload = {
            "offset": self._offset,
            "timeout": 5,
            "allowed_updates": ["message"]
        }
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            data = resp.json()
            if not data.get("ok"):
                logger.error(f"[TelegramWebhook] API错误: {data}")
                return

            for update in data.get("result", []):
                self._offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue

                # 过滤：只处理来自指定 chat_id 的消息
                msg_chat_id = str(message["chat"]["id"])
                if msg_chat_id != self.chat_id:
                    continue

                text = message.get("text", "").strip()
                if not text:
                    continue

                logger.info(f"[TelegramWebhook] 收到消息: {text}")

                # 解析命令和参数
                parts = text.split(maxsplit=1)
                command = parts[0].lstrip("/").lower()
                args = parts[1] if len(parts) > 1 else ""

                # 交给控制器处理
                reply = self.controller.handle_command(command, args)
                logger.info(f"[TelegramWebhook] 已回复: {reply[:80]}...")

                # 发送回复
                self._send_reply(reply)

        except requests.exceptions.Timeout:
            pass  # 超时是正常的
        except Exception as e:
            logger.error(f"[TelegramWebhook] 请求失败: {e}")

    def _send_reply(self, text: str):
        """发送回复消息到相同聊天"""
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        try:
            self.session.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"[TelegramWebhook] 回复发送失败: {e}")