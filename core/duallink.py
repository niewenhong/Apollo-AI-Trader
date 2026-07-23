"""
core/duallink.py - v2.6.0
双链路健康检查调度器（只读，不下单）
"""
import time
from datetime import datetime


class DualLink:
    """双链路管理器：定期检查美股/港股行情与交易接口存活"""

    def __init__(self, main_us=None, main_hk=None, db=None):
        self.main_us = main_us
        self.main_hk = main_hk
        self.db = db
        self._running = False

    def check_us(self) -> bool:
        if not self.main_us:
            return False
        try:
            return self.main_us.is_connected("FUTU")
        except Exception:
            return False

    def check_hk(self) -> bool:
        if not self.main_hk:
            return False
        try:
            return self.main_hk.is_connected("FUTU")
        except Exception:
            return False

    def health(self) -> dict:
        return {
            "us_md": self.check_us(),
            "hk_md": self.check_hk(),
            "ts": datetime.now().isoformat()
        }

    def start(self):
        self._running = True

    def stop(self):
        self._running = False