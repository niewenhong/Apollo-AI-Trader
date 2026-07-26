"""
strategies/equity/dual_thrust_strategy.py - v2.8.0
Dual Thrust 开盘区间突破策略
v2.8.0 优化：继承 ApolloBaseStrategy，统一接口规范
"""
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction

from strategies.base_strategy import ApolloBaseStrategy


class DualThrustStrategy(ApolloBaseStrategy):
    """Dual Thrust 开盘区间突破策略"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "lookback_period",      # 回看周期（计算区间用）
        "kshort",               # 上轨系数（做多突破）
        "klong",                # 下轨系数（做空突破）
        "atr_period",           # ATR 周期
        "use_atr_range",        # 用ATR代替固定区间
        "session_open_hour",    # 开盘时间（小时）
        "session_open_minute",  # 开盘时间（分钟）
    ]
    variables = ApolloBaseStrategy.variables + [
        "upper_band", "lower_band",
        "range_val", "open_price",
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
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.upper_band = 0.0
        self.lower_band = 0.0
        self.range_val = 0.0
        self.open_price = 0.0
        self._session_open = False
        self._daily_highest = 0.0
        self._daily_lowest = 0.0

        from vnpy.trader.utility import BarGenerator, ArrayManager
        self.bg = BarGenerator(self.on_bar, 1, self.on_1m_bar)
        self.am = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"DualThrust初始化 | 回看={self.lookback_period} kshort={self.kshort} klong={self.klong}")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg.update_bar(bar)

    def on_1m_bar(self, bar: BarData):
        """1分钟K线核心逻辑"""
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        close = bar.close_price

        # 每日开盘初始化
        if self._is_session_open(bar.datetime):
            if not self._session_open:
                self._session_open = True
                self._daily_highest = bar.high_price
                self._daily_lowest = bar.low_price
                self.open_price = close

                # 计算当日开盘区间
                self._calc_range(am)
                self.write_log(
                    f"📊 开盘区间 | open={self.open_price:.2f} "
                    f"range={self.range_val:.2f} "
                    f"upper={self.upper_band:.2f} lower={self.lower_band:.2f}"
                )
        else:
            self._session_open = False
            self._daily_highest = max(self._daily_highest, bar.high_price)
            self._daily_lowest = min(self._daily_lowest, bar.low_price)

        # 区间未计算则不交易
        if self.range_val <= 0:
            return

        # ── 持仓管理 ──
        if self.pos > 0:
            # 多头止损
            if close <= self.lower_band:
                self.sell(close, abs(self.pos))
                self.write_log(f"🔁 DualThrust反转 | 多→空 @ {close:.2f}")
                # 反转开空
                self.short(close, self.fixed_size)
                return
            if self.update_trailing_stop(close):
                self.sell(close, abs(self.pos))
                self.write_log(f"🛡️ DualThrust止损(多) @ {close:.2f}")
                return

        elif self.pos < 0:
            if close >= self.upper_band:
                self.cover(close, abs(self.pos))
                self.write_log(f"🔁 DualThrust反转 | 空→多 @ {close:.2f}")
                self.buy(close, self.fixed_size)
                return
            if self.update_trailing_stop(close):
                self.cover(close, abs(self.pos))
                self.write_log(f"🛡️ DualThrust止损(空) @ {close:.2f}")
                return

        # ── 开仓 ──
        else:
            if close > self.upper_band:
                self.buy(close, self.fixed_size)
                self.write_log(f"🟢 突破做多 | close={close:.2f} > upper={self.upper_band:.2f}")
            elif close < self.lower_band:
                self.short(close, self.fixed_size)
                self.write_log(f"🔴 突破做空 | close={close:.2f} < lower={self.lower_band:.2f}")

    def _calc_range(self, am):
        """计算开盘区间（最高-最低 或 ATR）"""
        if self.use_atr_range and len(am.high) >= self.atr_period:
            atr = am.atr(self.atr_period, array=False)
            self.range_val = atr
        elif len(am.high) >= self.lookback_period:
            period_high = float(np.max(am.high[-self.lookback_period:]))
            period_low = float(np.min(am.low[-self.lookback_period:]))
            self.range_val = period_high - period_low
        else:
            self.range_val = 0.0

        self.upper_band = self.open_price + self.range_val * self.kshort
        self.lower_band = self.open_price - self.range_val * self.klong

    def _is_session_open(self, bar_datetime) -> bool:
        """判断是否为开盘时刻"""
        h = bar_datetime.hour if hasattr(bar_datetime, 'hour') else bar_datetime.get('hour', 0)
        m = bar_datetime.minute if hasattr(bar_datetime, 'minute') else bar_datetime.get('minute', 0)
        return (h == self.session_open_hour and m >= self.session_open_minute)

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 DT成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
