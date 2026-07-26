"""
monitoring/telegram_notifier.py — Telegram 通知器 v2.8.0

功能：
- 发送消息（纯文本 / Markdown / HTML）
- 自动附加机器标识
- 支持多机器/多用户扩展
- 工业级机器标识解析（None / str / object 三种来源）
"""
import logging
import time
import socket
from typing import Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("TelegramNotifier")

VALID_PARSE_MODES = {"MarkdownV2", "HTML", "Markdown"}


class TelegramNotifier:
    """
    Telegram 通知器

    :param token: Bot Token
    :param chat_id: 目标聊天 ID
    :param proxy: 代理地址（可选）
    :param machine_registry: 机器标识来源
        - None → 自动使用 socket.gethostname()
        - str → 直接使用
        - object → 优先 .tag 属性，其次 .tag() 方法
    """

    def __init__(self, token: str, chat_id: str,
                 proxy: Optional[str] = None,
                 machine_registry: Optional[Union[str, object]] = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"

        # HTTP Session（带重试）
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

        # ── 机器标识解析（工业级，绝不崩溃）──
        if machine_registry is None:
            self.tag = socket.gethostname()
        elif isinstance(machine_registry, str):
            self.tag = machine_registry
        else:
            if hasattr(machine_registry, 'tag'):
                attr = getattr(machine_registry, 'tag')
                self.tag = attr() if callable(attr) else attr
            else:
                logger.warning("machine_registry 对象无 tag 属性，使用 str() 作为标识")
                self.tag = str(machine_registry)

        logger.info(f"机器标识: {self.tag}")

    # ──────────────────────────────
    #  发送消息
    # ──────────────────────────────
    def send_message(self, text: str, parse_mode: Optional[str] = None) -> bool:
        """发送消息（核心方法）"""
        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode in VALID_PARSE_MODES:
            payload["parse_mode"] = parse_mode

        try:
            resp = self.session.post(url, json=payload, timeout=(10, 60))
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                return True
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

    # ──────────────────────────────
    #  通知接口（自动附加标识）
    # ──────────────────────────────
    def notify(self, level: str, message: str, strategy_name: str = "") -> bool:
        """统一通知接口，自动附加机器标识"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        header = f"[{timestamp}] [{level}]"
        if strategy_name:
            header += f" [{strategy_name}]"
        full_text = f"{header} {self.tag} {message}"
        return self.send_message(full_text)

    def send_notification(self, symbol: str, price: float,
                          title: str, detail: str,
                          level: str = "info") -> bool:
        """结构化通知（供策略/系统调用）"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{timestamp}] [{level}] {self.tag}\n{title}\n{symbol} @ {price:.2f}\n{detail}"
        return self.send_message(text)
