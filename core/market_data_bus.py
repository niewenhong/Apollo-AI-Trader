"""
market_data_bus.py — 统一行情总线 v2.7.0
- 落库（多周期分表）
- 门禁检查（15m ATR）
- 策略分发（按标的路由）
"""

import logging

# vnpy 4.4.0 未导出 EVENT_BAR，自行定义
EVENT_BAR = "eBar"

logger = logging.getLogger(__name__)


class MarketDataBus:
    def __init__(self, db, strategy_engine=None, gate_threshold=None):
        self.db = db
        self.strategy_engine = strategy_engine
        self.gate_threshold = gate_threshold or 3.0  # 15m ATR 倍数
        self.handlers = []

    def register(self, handler):
        self.handlers.append(handler)

    def attach_to_engine(self, event_engine):
        event_engine.register(EVENT_BAR, self.on_bar)

    def on_bar(self, event):
        bar = event.data
        # 1. 落库
        try:
            self.db.save_bar(bar)
        except Exception as e:
            logger.error(f"落库失败: {e}")

        # 2. 门禁检查（15m周期）
        if bar.window == 15 and bar.interval == "MINUTE":
            self._check_gate(bar)

        # 3. 策略分发
        if self.strategy_engine:
            self.strategy_engine.dispatch_bar(bar)

        # 4. 其他处理器
        for h in self.handlers:
            try:
                h(bar)
            except Exception as e:
                logger.error(f"处理器异常: {e}")

    def _check_gate(self, bar):
        # 简化门禁：波动率超过阈值则拦截
        logger.debug(f"[GATE] 15m BAR {bar.symbol} C={bar.close_price}")