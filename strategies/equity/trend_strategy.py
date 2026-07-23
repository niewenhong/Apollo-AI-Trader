"""
strategies/equity/trend_strategy.py - Apollo-AI-Trader v2.6.0
长周期趋势跟踪：MA60/MA200 金叉死叉 + ADX过滤
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData
import numpy as np


class TrendStrategy(CtaTemplate):
    author = "Apollo"
    version = "v2.6.0"

    fast_ma = 60
    slow_ma = 200
    adx_period = 14
    adx_threshold = 20
    atr_period = 14
    atr_stop_multiplier = 3.0
    fixed_size = 100

    parameters = ["fast_ma","slow_ma","adx_period","adx_threshold",
                  "atr_period","atr_stop_multiplier","fixed_size"]
    variables = ["pos","entry_price","trailing_stop"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0; self.entry_price = 0.0; self.trailing_stop = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=max(self.slow_ma+10, 250))

    def on_init(self): self.load_bar(30, use_database=True)
    def on_start(self): self.write_log(f"[Trend] start {self.vt_symbol}")
    def on_stop(self): self.write_log(f"[Trend] stop {self.vt_symbol}")
    def on_tick(self, t): self.bg.update_tick(t)

    def on_bar(self, bar: BarData):
        am = self.am; am.update_bar(bar)
        if not am.inited: return
        ma_f = np.mean(am.close[-self.fast_ma:])
        ma_s = np.mean(am.close[-self.slow_ma:])
        atr = am.atr(self.atr_period)
        # 简化ADX
        adx = self._calc_adx(am.high[-30:], am.low[-30:], am.close[-30:], self.adx_period)
        # 入场
        if self.pos == 0:
            if ma_f > ma_s and adx > self.adx_threshold:
                self.buy(bar.close_price+0.01, self.fixed_size)
            elif ma_f < ma_s and adx > self.adx_threshold:
                self.short(bar.close_price-0.01, self.fixed_size)
        # 出场：ATR跟踪止损
        elif self.pos > 0:
            new_stop = bar.close_price - atr * self.atr_stop_multiplier
            self.trailing_stop = max(self.trailing_stop, new_stop)
            if bar.close_price < self.trailing_stop:
                self.sell(bar.close_price-0.01, abs(self.pos))
        elif self.pos < 0:
            new_stop = bar.close_price + atr * self.atr_stop_multiplier
            self.trailing_stop = min(self.trailing_stop, new_stop)
            if bar.close_price > self.trailing_stop:
                self.cover(bar.close_price+0.01, abs(self.pos))

    def on_order(self, order):
        if order.traded > 0:
            if order.direction.name == "LONG": self.pos = order.traded; self.entry_price = order.price
            else: self.pos = -order.traded
            self.trailing_stop = order.price

    @staticmethod
    def _calc_adx(high, low, close, n):
        if len(close) < n*2: return 15.0
        plus_dm = np.maximum(np.diff(high),0)
        minus_dm = np.maximum(-np.diff(low),0)
        tr = np.maximum(np.abs(np.diff(close)), np.abs(close[1:]-close[:-1]))
        atr = np.mean(tr[-n:])
        pdi = 100*np.mean(plus_dm[-n:])/(atr+1e-6)
        mdi = 100*np.mean(minus_dm[-n:])/(atr+1e-6)
        dx = 100*abs(pdi-mdi)/(pdi+mdi+1e-6)
        return float(np.mean(dx[-n:]))
