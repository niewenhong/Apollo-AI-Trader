"""
duallink.py — Apollo-AI-Tra-der v2.5.0-DEBUG
双链路调度器（最小可用版）
仅做链路存活检查 + 定时 query，不做订单操作
"""
import time
import logging
from threading import Thread

logger = logging.getLogger("DualLink")


class DualLink:
    """双链路调度器 — 链路存活检查 + 定时 query"""

    def __init__(self, main_engine, gateways: dict, remote_controller=None):
        self.main_engine = main_engine
        self.gateways = gateways  # {"US": gw_us, "HK": gw_hk}
        self.rc = remote_controller
        self._running = False
        self._thread = None
        self.interval = 30  # 秒
        self.review_count = 0

    def start(self):
        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"[DualLink] 双链路调度器已启动 | interval={self.interval}s")

    def stop(self):
        self._running = False
        logger.info("[DualLink] 双链路调度器已停止")

    def _run(self):
        while self._running:
            try:
                self._review()
            except Exception as e:
                logger.warning(f"[DualLink] REVIEW 异常: {e}")
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

    def _review(self):
        """链路健康检查 — 只读，不操作订单"""
        self.review_count += 1
        for tag, gw in self.gateways.items():
            ctx = getattr(gw, 'trade_ctx', None)
            quote_ctx = getattr(gw, 'quote_ctx', None)
            trade_alive = ctx is not None
            quote_alive = quote_ctx is not None

            try:
                gw.query_account()
            except Exception as e:
                logger.warning(f"[DualLink] {tag} query_account 失败: {e}")

            logger.info(f"[DualLink] 🔍 {tag} REVIEW #{self.review_count} | "
                        f"trade={'✅' if trade_alive else '❌'} | "
                        f"quote={'✅' if quote_alive else '❌'}")

            if not trade_alive or not quote_alive:
                logger.warning(f"[DualLink] ⚠️ {tag} 链路异常，需重连")
                # TODO: 重连逻辑
                # gw.connect(...)
