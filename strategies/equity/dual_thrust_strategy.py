"""
strategies/equity/dual_thrust_strategy.py - Apollo-AI-Trader v2.6.0
Dual Thrust 开盘区间突破策略（从market_switcher_dual提炼纯策略逻辑）
- 计算N日HH/HC/LC/LL确定Range
- 上轨=Open+K1*Range, 下轨=Open-K2*Range
- 均线斜率动态调整K1/K2
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Offset
import numpy as np
from datetime import time as dtime


class DualThrustStrategy(CtaTemplate):
    author = "Apollo"
    version = "v2.6.0"

    # 参数
    lookback = 5               # 回看天数
    k1 = 0.5                   # 上轨系数
    k2 = 0.5                   # 下轨系数
    ma_period = 20             # 均线周期
    ma_slope_adjust = True     # 均线斜率调整
    fixed_size = 100
    stop_loss_pct = 0.02
    take_profit_pct = 0.04
    time_exit_hour = 23        # 强制平仓时间（港股23点前）
    time_exit_minute = 55

    parameters = [
        "lookback", "k1", "k2", "ma_period", "ma_slope_adjust",
        "fixed_size", "stop_loss_pct", "take_profit_pct",
        "time_exit_hour", "time_exit_minute",
    ]
    variables = ["pos", "range_val", "upper_band", "lower_band",
                 "entry_price", "highest", "lowest"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.range_val = 0.0
        self.upper_band = 0.0
        self.lower_band = 0.0
        self.entry_price = 0.0
        self.highest = 0.0
        self.lowest = 999999.0
        self.bg = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am = ArrayManager(size=max(self.lookback*48, 100))
        self._today_open = 0.0
        self._today_range = None

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log(f"[DualThrust] on_init | {self.vt_symbol}")

    def on_start(self):
        self.write_log(f"[DualThrust] on_start | {self.vt_symbol}")

    def on_stop(self):
        self.write_log(f"[DualThrust] on_stop")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg.update_bar(bar)

    def on_5min_bar(self, bar: BarData):
        """5分钟Bar计算Range和信号"""
        am = self.am
        am.update_bar(bar)
        if not am.inited: return

        # 每日开盘时计算Range
        now = bar.datetime
        if now.time() < dtime(9,35):
            self._calc_today_range(am)
            self._today_open = bar.open_price

        # 检查止损止盈
        if self.pos > 0:
            if bar.close_price <= self.entry_price * (1-self.stop_loss_pct):
                self.sell(bar.close_price-0.01, abs(self.pos)); return
            if bar.close_price >= self.entry_price * (1+self.take_profit_pct):
                self.sell(bar.close_price-0.01, abs(self.pos)); return
        if self.pos < 0:
            if bar.close_price >= self.entry_price * (1+self.stop_loss_pct):
                self.cover(bar.close_price+0.01, abs(self.pos)); return

        # 收盘前强平
        if now.hour >= self.time_exit_hour and now.minute >= self.time_exit_minute:
            if self.pos > 0: self.sell(bar.close_price-0.01, abs(self.pos))
            if self.pos < 0: self.cover(bar.close_price+0.01, abs(self.pos))
            return

        # 突破信号
        if self.range_val > 0:
            upper = self._today_open + self.k1 * self.range_val
            lower = self._today_open - self.k2 * self.range_val
            if bar.close_price > upper and self.pos <= 0:
                self.buy(bar.close_price+0.01, self.fixed_size)
            elif bar.close_price < lower and self.pos >= 0:
                self.short(bar.close_price-0.01, self.fixed_size)

    def _calc_today_range(self, am):
        """计算N日Range = Max(HH-LC, HC-LL)"""
        closes = am.close[-self.lookback*48:] if len(am.close)>=self.lookback*48 else am.close
        highs = am.high[-self.lookback*48:] if len(am.high)>=self.lookback*48 else am.high
        lows = am.low[-self.lookback*48:] if len(am.low)>=self.lookback*48 else am.low
        if len(closes) < 2: return
        hh = np.max(highs[:-1])
        lc = np.min(closes[:-1])
        hc = np.max(closes[:-1])
        ll = np.min(lows[:-1])
        rng = max(hh-lc, hc-ll)
        # 均线斜率调整
        if self.ma_slope_adjust and len(closes) >= self.ma_period:
            ma = np.mean(closes[-self.ma_period:])
            prev_ma = np.mean(closes[-self.ma_period-5:-5])
            slope = (ma-prev_ma)/prev_ma if prev_ma>0 else 0
            if slope > 0.002:   # 上升市：偏多
                self.k1 *= 0.8; self.k2 *= 1.2
            elif slope < -0.002: # 下降市：偏空
                self.k1 *= 1.2; self.k2 *= 0.8
        self.range_val = float(rng)
        self.write_log(f"[DualThrust] Range={rng:.2f} K1={self.k1:.2f} K2={self.k2:.2f}")

    def on_order(self, order):
        if order.traded > 0:
            if order.direction == Direction.LONG:
                self.pos = order.traded
                self.entry_price = order.price
            elif order.direction == Direction.SHORT:
                self.pos = -order.traded
                self.entry_price = order.price
        self.put_event()
