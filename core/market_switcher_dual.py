"""
core/market_switcher_dual.py — Apollo-AI-Trader v2.5.0-FINAL
双链路调度器（最小可用版）
"""
import logging
import time
from datetime import datetime

logger = logging.getLogger("DualLink")


class DualLinkOrchestrator:
    """双链路调度器：US / HK 两套 CtaEngine 独立调度"""

    def __init__(self, cta_us, cta_hk, config: dict):
        self.cta_us = cta_us
        self.cta_hk = cta_hk
        self.config = config
        self.running = False
        self.thread = None
        logger.info("[DualLink] 双链路调度器初始化完成")

    def start(self):
        self.running = True
        self.thread = __import__("threading").Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("[DualLink] 双链路调度器已启动")

    def stop(self):
        self.running = False
        logger.info("[DualLink] 双链路调度器已停止")

    def _run(self):
        """后台循环：按时间切换 US / HK 链路"""
        while self.running:
            now = datetime.now()
            # 盘前准备 / 盘后复盘 的简单判断
            if 9 <= now.hour < 12:
                logger.info("[DualLink] 🔍 HK TRADING")
            elif 21 <= now.hour or now.hour < 4:
                logger.info("[DualLink] 🔍 US TRADING")
            else:
                logger.info("[DualLink] 🔍 REVIEW")
            time.sleep(60)
