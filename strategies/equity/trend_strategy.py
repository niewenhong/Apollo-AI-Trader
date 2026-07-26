"""
strategies/equity/trend_strategy.py - v2.8.0
趋势跟踪策略：均线突破 + ATR 止损 + 趋势确认
v2.8.0 优化：继承 ApolloBaseStrategy，统一接口规范
"""
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction

from strategies.base_strategy import ApolloBaseStrategy


class TrendStrategy(ApolloBaseStrategy):
    """趋势跟踪策略：双均线 + 突破确认 + ATR 止损"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "ma_fast",              # 快线周期
        "ma_slow",              # 慢线周期
        "breakout_period",      # 突破确认周期
        "atr_period",           # ATR 周期
        "atr_stop_multiplier",  # ATR 止损倍数
        "adx_period",           # ADX 周期
        "adx_threshold",        # ADX 趋势强度阈值
        "use_trailing",         # 是否使用移动止盈
    ]
    variables = ApolloBaseStrategy.variables + [
        "ma_fast_val", "ma_slow_val", "adx_val",
        "atr_val", "trend_direction",
    ]

    DEFAULTS = {
        **ApolloBaseStrategy.DEFAULTS,
        "ma_fast": 10,
        "ma_slow": 30,
        "breakout_period": 3,
        "atr_period": 14,
        "atr_stop_multiplier": 2.5,
        "adx_period": 14,
        "adx_threshold": 20,
        "use_trailing": True,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.ma_fast_val = 0.0
        self.ma_slow_val = 0.0
        self.adx_val = 0.0
        self.atr_val = 0.0
        self.trend_direction = 0  # 1=多, -1=空, 0=无

        # K线工具
        from vnpy.trader.utility import BarGenerator, ArrayManager
        self.bg = BarGenerator(self.on_bar, 1, self.on_1m_bar)
        self.am = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"趋势策略初始化 | 快线={self.ma_fast} 慢线={self.ma_slow}")

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

        # 计算指标
        self.ma_fast_val = am.sma(self.ma_fast, array=False)
        self.ma_slow_val = am.sma(self.ma_slow, array=False)
        self.atr_val = am.atr(self.atr_period, array=False)

        if len(am.close) >= self.adx_period:
            self.adx_val = am.adx(self.adx_period, array=False)
        else:
            self.adx_val = 0.0

        # 趋势方向判断
        trend = 0
        if self.ma_fast_val > self.ma_slow_val and self.adx_val > self.adx_threshold:
            trend = 1  # 上升趋势
        elif self.ma_fast_val < self.ma_slow_val and self.adx_val > self.adx_threshold:
            trend = -1  # 下降趋势

        # 突破确认：连续 N 根K线方向一致
        breakout_up = self._check_breakout(am.close, self.breakout_period, direction=1)
        breakout_down = self._check_breakout(am.close, self.breakout_period, direction=-1)

        # ── 交易决策 ──
        if self.pos == 0:
            if trend == 1 and breakout_up:
                self.buy(close, self.fixed_size)
                self.write_log(f"🟢 趋势做多 | MA金叉+突破 | score=UP adx={self.adx_val:.0f}")
            elif trend == -1 and breakout_down:
                self.short(close, self.fixed_size)
                self.write_log(f"🔴 趋势做空 | MA死叉+突破 | score=DOWN adx={self.adx_val:.0f}")

        elif self.pos > 0:
            # 多头止损/止盈
            if self.use_trailing:
                if self.update_trailing_stop(close):
                    self.sell(close, abs(self.pos))
                    self.write_log(f"🛡️ 趋势多头止损/止盈 @ {close:.2f}")
                    return
            else:
                hard_stop = self.entry_price - self.atr_val * self.atr_stop_multiplier
                if close <= hard_stop:
                    self.sell(close, abs(self.pos))
                    self.write_log(f"🛡️ ATR硬止损(多) @ {close:.2f}")
                    return

            # 趋势反转平仓
            if trend == -1 and breakout_down:
                self.sell(close, abs(self.pos))
                self.write_log(f"🔁 趋势反转平仓(多)")

        elif self.pos < 0:
            # 空头止损
            if self.use_trailing:
                if self.update_trailing_stop(close):
                    self.cover(close, abs(self.pos))
                    self.write_log(f"🛡️ 趋势空头止损/止盈 @ {close:.2f}")
                    return
            else:
                hard_stop = self.entry_price + self.atr_val * self.atr_stop_multiplier
                if close >= hard_stop:
                    self.cover(close, abs(self.pos))
                    self.write_log(f"🛡️ ATR硬止损(空) @ {close:.2f}")
                    return

            # 趋势反转平仓
            if trend == 1 and breakout_up:
                self.cover(close, abs(self.pos))
                self.write_log(f"🔁 趋势反转平仓(空)")

        self.trend_direction = trend

    def _check_breakout(self, close_arr, period: int, direction: int) -> bool:
        """检查连续 period 根K线是否同向"""
        if len(close_arr) < period + 1:
            return False
        recent = close_arr[-period-1:]
        if direction == 1:
            return all(recent[i] > recent[i-1] for i in range(1, len(recent)))
        else:
            return all(recent[i] < recent[i-1] for i in range(1, len(recent)))

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 趋势成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
