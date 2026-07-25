"""
market_data_bus.py — 统一行情总线 v2.7.0
- 落库（多周期分表）
- 门禁检查（15m ATR）
- 策略分发（按标的路由）
"""

import logging
from vnpy.trader.event import EVENT_BAR

logger = logging.getLogger(__name__)


class MarketDataBus:
    def __init__(self, db, strategy_engine=None, gate_threshold=None):
        self.db = db
        self.strategy_engine = strategy_engine
        self.gate_threshold = gate_threshold or 3.0
        self.handlers = []
        self._atr_cache = {}  # symbol -> 最近N根15m BAR

    def register(self, handler):
        self.handlers.append(handler)

    def attach_to_engine(self, event_engine):
        event_engine.register(EVENT_BAR, self.on_bar)
        logger.info("✅ MarketDataBus 已注册到 EventEngine")

    def on_bar(self, event):
        bar = event.data
        symbol = bar.symbol

        # 1. 落库
        try:
            self.db.save_bar(bar)
        except Exception as e:
            logger.error(f"落库失败 {symbol}: {e}")

        # 2. 门禁检查（15m周期）
        if getattr(bar, 'window', 0) == 15 and bar.interval == "MINUTE":
            self._check_gate(bar)

        # 3. 策略分发
        if self.strategy_engine:
            try:
                self.strategy_engine.dispatch_bar(bar)
            except Exception as e:
                logger.error(f"策略分发异常: {e}")

        # 4. 自定义处理器
        for h in self.handlers:
            try:
                h(bar)
            except Exception as e:
                logger.error(f"处理器异常: {e}")

    def _check_gate(self, bar):
        """15m ATR门禁检查"""
        sym = bar.symbol
        if sym not in self._atr_cache:
            self._atr_cache[sym] = []
        self._atr_cache[sym].append(bar)
        if len(self._atr_cache[sym]) > 20:
            self._atr_cache[sym].pop(0)

        bars = self._atr_cache[sym]
        if len(bars) < 14:
            return  # 数据不足，不判断

        # 计算简易ATR
        trs = []
        for i in range(1, len(bars)):
            h = bars[i].high_price
            l = bars[i].low_price
            pc = bars[i-1].close_price
            tr = max(h-l, abs(h-pc), abs(l-pc))
            trs.append(tr)
        atr = sum(trs) / len(trs) if trs else 0

        # 当前BAR的波动率
        if len(bars) >= 2:
            prev_close = bars[-2].close_price
            curr_range = (bar.high_price - bar.low_price) / prev_close if prev_close > 0 else 0
            threshold = atr / prev_close * self.gate_threshold if prev_close > 0 else 0
            if curr_range > threshold:
                logger.warning(f"🚧 [GATE] {sym} 15m波动率异常: {curr_range:.4f} > {threshold:.4f}")
            else:
                logger.debug(f"[GATE] {sym} 15m OK (atr={atr:.4f})")
