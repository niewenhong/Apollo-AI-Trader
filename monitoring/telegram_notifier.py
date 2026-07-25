"""
monitoring/telegram_notifier.py — Telegram 通知器 v2.7.0
功能：向指定 chat 发送文本/HTML 消息，带重试，支持持仓/账户查询
版本：v2.7.0
变更：2026-07-26 新增 handle_positions_query / handle_account_query 方法
"""

import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("Telegram")
VALID_PARSE_MODES = {"MarkdownV2", "HTML", "Markdown"}


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, proxy: Optional[str] = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

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
                logger.debug(f"[Telegram] 发送成功: {text[:50]}")
                return True
            logger.error(f"[Telegram] API错误: {data}")
            return False
        except Exception as e:
            logger.error(f"[Telegram] 发送异常: {e}")
            return False

    def send_reply(self, chat_id: str, text: str) -> bool:
        return self.send_message(text)

    def send_markdown(self, text: str) -> bool:
        return self.send_message(text, parse_mode="MarkdownV2")

    def send_html(self, text: str) -> bool:
        return self.send_message(text, parse_mode="HTML")

    def notify(self, level: str, message: str, strategy_name: str = "") -> bool:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        header = f"[{ts}] [{level}]"
        if strategy_name:
            header += f" [{strategy_name}]"
        return self.send_message(f"{header} {message}")

    # ---------- 新增方法：处理持仓/账户查询 ----------
    def handle_positions_query(self, main_engine) -> str:
        """处理持仓查询请求"""
        if not main_engine:
            return "⚠️ 交易引擎未连接"
        positions = main_engine.get_all_positions()
        if not positions:
            return "📭 当前无持仓"
        lines = ["📊 当前持仓:"]
        for p in positions:
            lines.append(
                f"  {p.symbol} | 方向:{p.direction.value} | "
                f"量:{p.volume} | 均价:{p.price:.2f} | 盈亏:{p.pnl:.2f}"
            )
        return "\n".join(lines)

    def handle_account_query(self, main_engine) -> str:
        """处理账户资金查询请求"""
        if not main_engine:
            return "⚠️ 交易引擎未连接"
        accounts = main_engine.get_all_accounts()
        if not accounts:
            return "⚠️ 未获取到账户信息"
        lines = ["💰 账户资金:"]
        for acc in accounts:
            lines.append(f"  {acc.accountid} 余额={acc.balance:,.2f} 冻结={acc.frozen:,.2f}")
        return "\n".join(lines)