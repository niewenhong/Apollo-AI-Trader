"""
monitoring/telegram_notifier.py - Telegram 通知器
修复：parse_mode 仅当为有效值时加入 payload，避免 API 报错
"""
import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("TelegramNotifier")

VALID_PARSE_MODES = {"MarkdownV2", "HTML", "Markdown"}


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, proxy: Optional[str] = None):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}
        self._running = False

    def send_message(self, text: str, parse_mode: Optional[str] = None) -> bool:
        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode in VALID_PARSE_MODES:
            payload["parse_mode"] = parse_mode

        try:
            resp = self.session.post(url, json=payload, timeout=(10, 60))
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                logger.debug(f"[Telegram] 消息发送成功: {text[:50]}...")
                return True
            else:
                logger.error(f"[Telegram] API 错误: {data}")
                return False
        except requests.exceptions.Timeout:
            logger.error("[Telegram] 请求超时")
            return False
        except Exception as e:
            logger.error(f"[Telegram] 发送异常: {e}")
            return False

    def send_markdown(self, text: str) -> bool:
        return self.send_message(text, parse_mode="MarkdownV2")

    def send_html(self, text: str) -> bool:
        return self.send_message(text, parse_mode="HTML")

    def notify(self, level: str, message: str, strategy_name: str = "") -> bool:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        header = f"[{timestamp}] [{level}]"
        if strategy_name:
            header += f" [{strategy_name}]"
        full_text = f"{header} {message}"
        return self.send_message(full_text)