"""
core/duallink.py - Apollo-AI-Trader v2.6.0
DualLink 双链路调度器：US/HK 市场切换 + 交易时段管理
"""
import logging
import time
import threading
from datetime import datetime, time as dtime
from typing import Dict

logger = logging.getLogger("DualLink")


class DualLink:
    """双市场链路调度器"""

    def __init__(self, main_engines: dict, gateways: dict, rc=None):
        self.engines = main_engines  # {"US": main_us, "HK": main_hk}
        self.gateways = gateways
        self.rc = rc
        self._stop = False
        self._thread = None
        self.current_market = None
        self.extended_hours = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("[DualLink] 启动")

    def stop(self):
        self._stop = True

    def _run(self):
        while not self._stop:
            try:
                self._check_and_switch()
                time.sleep(30)  # 30秒检查一次
            except Exception as e:
                logger.error(f"[DualLink] {e}")
                time.sleep(60)

    def _check_and_switch(self):
        now = datetime.now()
        weekday = now.weekday()  # 0=Mon
        if weekday >= 5:
            self._switch_to("CLOSED")
            return
        # 港股交易时段（HKT = UTC+8）
        hk_open = dtime(9,30); hk_close = dtime(16,0)
        # 美股交易时段（EST = UTC-5, 夏令时UTC-4）
        # 简化：用本地时间估算
        us_open = dtime(21,30); us_close = dtime(4,0)  # 近似
        current = now.time()
        if hk_open <= current <= hk_close:
            self._switch_to("HK")
        elif current >= us_open or current <= us_close:
            self._switch_to("US")
        else:
            # 盘前盘后
            self._switch_to("EXTENDED")

    def _switch_to(self, market: str):
        if self.current_market == market: return
        logger.info(f"[DualLink] 切换: {self.current_market} → {market}")
        self.current_market = market
        # 通知RC
        if self.rc and hasattr(self.rc, '_on_market_switch'):
            self.rc._on_market_switch(market)
        # 可以在此暂停/恢复对应市场策略
        if market == "HK":
            # 激活HK策略
            pass
        elif market == "US":
            pass
        elif market == "EXTENDED":
            self.extended_hours = True
        elif market == "CLOSED":
            self.extended_hours = False
