"""
strategies/equity/trend_strategy.py - v2.6.0
长周期趋势策略：基于周线/日线级别均线交叉，捕捉中期趋势
实验状态：适用于趋势明显的市场环境
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Interval
import numpy as np


class TrendStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "trend_ma_fast",      # 快线周期（日线）
        "trend_ma_slow",      # 慢线周期（日线）
        "entry_ma_fast",      # 入场快线（小时线）
        "entry_ma_slow",      # 入场慢线（小时线）
        "atr_period",         # ATR周期
        "atr_multiplier",     # ATR止损倍数
        "fixed_size",
    ]

    variables = [
        "pos", "entry_price", "trend_direction",
        "fast_ma", "slow_ma", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.trend_direction = 0  # 1: uptrend, -1: downtrend, 0: neutral
        self.fast_ma = 0.0
        self.slow_ma = 0.0
        self.pnl = 0.0

        # 多时间框架
        self.bg_daily = BarGenerator(self.on_bar, 1, self.on_daily_bar)
        self.am_daily = ArrayManager(size=100)
        self.bg_hourly = BarGenerator(self.on_bar, 60, self.on_hourly_bar)
        self.am_hourly = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(30, use_database=True)
        self.write_log("Trend策略初始化完成")

    def on_start(self):
        self.write_log("Trend策略启动")

    def on_stop(self):
        self.write_log("Trend策略停止")

    def on_tick(self, tick: TickData):
        self.bg_daily.update_tick(tick)
        self.bg_hourly.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg_daily.update_bar(bar)
        self.bg_hourly.update_bar(bar)

    def on_daily_bar(self, bar: BarData):
        """日线确定趋势方向"""
        self.am_daily.update_bar(bar)
        if not self.am_daily.inited:
            return

        self.fast_ma = self.am_daily.sma(self.trend_ma_fast, array=False)
        self.slow_ma = self.am_daily.sma(self.trend_ma_slow, array=False)

        if self.fast_ma > self.slow_ma:
            self.trend_direction = 1
        elif self.fast_ma < self.slow_ma:
            self.trend_direction = -1
        else:
            self.trend_direction = 0

    def on_hourly_bar(self, bar: BarData):
        """小时线入场/出场"""
        self.am_hourly.update_bar(bar)
        if not self.am_hourly.inited or self.trend_direction == 0:
            return

        entry_fast = self.am_hourly.sma(self.entry_ma_fast, array=False)
        entry_slow = self.am_hourly.sma(self.entry_ma_slow, array=False)
        atr = self.am_hourly.atr(self.atr_period, array=False)

        if self.pos == 0:
            # 趋势向上且小时线金叉
            if self.trend_direction == 1 and entry_fast > entry_slow:
                self.buy(bar.close_price, self.fixed_size)
                self.entry_price = bar.close_price
                self.write_log(f"趋势多头入场: 日线上升+小时金叉")
            # 趋势向下且小时线死叉
            elif self.trend_direction == -1 and entry_fast < entry_slow:
                self.short(bar.close_price, self.fixed_size)
                self.entry_price = bar.close_price
                self.write_log(f"趋势空头入场: 日线下降+小时死叉")
        else:
            # 出场：趋势反转或止损
            if (self.pos > 0 and self.trend_direction == -1) or \
               (self.pos < 0 and self.trend_direction == 1):
                self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))
                self.write_log("趋势反转平仓")
            # ATR止损
            elif self.pos > 0 and bar.close_price < self.entry_price - atr * self.atr_multiplier:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log("ATR止损: 多头")
            elif self.pos < 0 and bar.close_price > self.entry_price + atr * self.atr_multiplier:
                self.cover(bar.close_price, abs(self.pos))
                self.write_log("ATR止损: 空头")