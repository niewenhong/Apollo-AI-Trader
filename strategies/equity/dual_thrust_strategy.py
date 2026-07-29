"""
strategies/equity/dual_thrust_strategy.py - v2.9.0
Dual Thrust 开盘区间突破 + 多周期确认 + Regime 感知
v2.9.0 优化：
- 继承 ApolloBaseStrategy
- 用 1M K线驱动（已订阅），不再合成
- ATR 动态区间 + 突破确认用 5M 收盘
- 反转逻辑 + 移动止盈
- 超时 + 止损
"""
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction

from strategies.base_strategy import ApolloBaseStrategy


class DualThrustStrategy(ApolloBaseStrategy):
    """Dual Thrust 开盘区间突破（v2.9.0）"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "lookback_period",
        "kshort",
        "klong",
        "atr_period",
        "use_atr_range",
        "session_open_hour",
        "session_open_minute",
        "use_5m_confirm",
    ]
    variables = ApolloBaseStrategy.variables + [
        "upper_band", "lower_band",
        "range_val", "open_price",
        "_5m_breakout_up", "_5m_breakout_down",
    ]

    DEFAULTS = {
        **ApolloBaseStrategy.DEFAULTS,
        "lookback_period": 10,
        "kshort": 0.5,
        "klong": 0.5,
        "atr_period": 14,
        "use_atr_range": True,
        "session_open_hour": 9,
        "session_open_minute": 30,
        "use_5m_confirm": True,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.upper_band = 0.0
        self.lower_band = 0.0
        self.range_val = 0.0
        self.open_price = 0.0
        self._5m_breakout_up = False
        self._5m_breakout_down = False

        self._session_open = False
        self._daily_highest = 0.0
        self._daily_lowest = 0.0

        from vnpy.trader.utility import ArrayManager
        self.am_5m = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"DualThrust初始化 | 回看={self.lookback_period} k={self.kshort}/{self.klong}")

    # ── 1M 层：执行 ──
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        if not self.am.inited:
            return

        close = bar.close_price

        # 每日开盘初始化
        if self._is_session_open(bar.datetime):
            if not self._session_open:
                self._session_open = True
                self._daily_highest = bar.high_price
                self._daily_lowest = bar.low_price
                self.open_price = close
                self._calc_range()
                self.write_log(f"📊 开盘区间 | open={self.open_price:.2f} range={self.range_val:.2f} upper={self.upper_band:.2f} lower={self.lower_band:.2f}")
        else:
            self._session_open = False
            self._daily_highest = max(self._daily_highest, bar.high_price)
            self._daily_lowest = min(self._daily_lowest, bar.low_price)

        if self.range_val <= 0:
            return

        # 持仓管理
        if self.pos > 0:
            if self.update_trailing_stop(close):
                self.sell(close, abs(self.pos))
                self.write_log(f"🛡️ DT止损/止盈(多) @ {close:.2f}")
                return
            if close <= self.lower_band:
                self.sell(close, abs(self.pos))
                self.write_log(f"🔁 DT反转 多→空 @ {close:.2f}")
                self.short(close, self.fixed_size)
                return
            if self.bars_held >= self.max_holding_bars:
                self.sell(close, abs(self.pos))
                self.write_log(f"⏰ DT超时(多) @ {close:.2f}")
                return

        elif self.pos < 0:
            if self.update_trailing_stop(close):
                self.cover(close, abs(self.pos))
                self.write_log(f"🛡️ DT止损/止盈(空) @ {close:.2f}")
                return
            if close >= self.upper_band:
                self.cover(close, abs(self.pos))
                self.write_log(f"🔁 DT反转 空→多 @ {close:.2f}")
                self.buy(close, self.fixed_size)
                return
            if self.bars_held >= self.max_holding_bars:
                self.cover(close, abs(self.pos))
                self.write_log(f"⏰ DT超时(空) @ {close:.2f}")
                return

        # 开仓（需 5M 确认 + Regime）
        else:
            if self.use_5m_confirm and not (self._5m_breakout_up or self._5m_breakout_down):
                return
            if not self.is_regime_tradeable():
                return
            allow_open, _ = self.check_time_window(bar.datetime)
            if not allow_open:
                return

            if close > self.upper_band:
                self.buy(close, self.fixed_size)
                self.write_log(f"🟢 DT突破做多 | {close:.2f} > {self.upper_band:.2f}")
            elif close < self.lower_band:
                self.short(close, self.fixed_size)
                self.write_log(f"🔴 DT突破做空 | {close:.2f} < {self.lower_band:.2f}")

    # ── 5M 层：突破确认 ──
    def on_5m_bar(self, bar: BarData):
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        close = bar.close_price
        # 用 5M 收盘价确认突破（过滤 1M 噪音）
        if self.range_val > 0:
            self._5m_breakout_up = close > (self.open_price + self.range_val * self.kshort)
            self._5m_breakout_down = close < (self.open_price - self.range_val * self.klong)

    # ── 区间计算 ──
    def _calc_range(self):
        if self.use_atr_range and len(self.am.high) >= self.atr_period:
            self.range_val = self.am.atr(self.atr_period, array=False)
        elif len(self.am.high) >= self.lookback_period:
            period_high = float(np.max(self.am.high[-self.lookback_period:]))
            period_low = float(np.min(self.am.low[-self.lookback_period:]))
            self.range_val = period_high - period_low
        else:
            self.range_val = 0.0
        self.upper_band = self.open_price + self.range_val * self.kshort
        self.lower_band = self.open_price - self.range_val * self.klong

    def _is_session_open(self, bar_datetime) -> bool:
        h = bar_datetime.hour if hasattr(bar_datetime, 'hour') else 0
        m = bar_datetime.minute if hasattr(bar_datetime, 'minute') else 0
        return (h == self.session_open_hour and m >= self.session_open_minute)

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 DT成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
