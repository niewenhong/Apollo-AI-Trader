"""
strategies/equity/vwap_strategy.py - v2.6.0
VWAP均值回归策略：基于实时逐笔数据计算VWAP，偏离过大时反向交易
支持双模式：Tick精确模式 / 1分钟K线近似模式
实验状态：需订阅Tick数据以获得最佳效果
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class VWAPStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "deviation_entry",    # 偏离VWAP的入场阈值（如0.003表示0.3%）
        "deviation_exit",     # 回归VWAP的出场阈值
        "fixed_size",
        "use_tick_mode",      # True: Tick精确模式, False: 1分钟K线近似
        "max_daily_trades",
    ]

    variables = [
        "pos", "vwap", "cum_volume", "cum_pv",
        "tick_count", "daily_trades", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.vwap = 0.0
        self.cum_volume = 0  # 当日累计成交量
        self.cum_pv = 0.0    # 当日累计 price * volume
        self.tick_count = 0
        self.daily_trades = 0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("VWAP策略初始化完成")

    def on_start(self):
        self.write_log("VWAP策略启动")

    def on_stop(self):
        self.write_log("VWAP策略停止")

    def on_tick(self, tick: TickData):
        """Tick驱动模式：实时更新VWAP"""
        if not self.use_tick_mode:
            self.bg.update_tick(tick)
            return

        # 更新累计值
        self.cum_volume += tick.volume
        self.cum_pv += tick.last_price * tick.volume
        self.vwap = self.cum_pv / self.cum_volume if self.cum_volume > 0 else tick.last_price
        self.tick_count += 1

        # 交易逻辑
        deviation = (tick.last_price - self.vwap) / self.vwap

        if self.pos == 0 and self.daily_trades < self.max_daily_trades:
            if deviation < -self.deviation_entry:  # 价格低于VWAP，买入
                self.buy(tick.last_price, self.fixed_size)
                self.daily_trades += 1
                self.write_log(f"VWAP买入: 偏离{deviation*100:.2f}%")
            elif deviation > self.deviation_entry:  # 价格高于VWAP，卖出
                self.short(tick.last_price, self.fixed_size)
                self.daily_trades += 1
                self.write_log(f"VWAP卖出: 偏离{deviation*100:.2f}%")
        elif self.pos != 0:
            # 回归VWAP时平仓
            if abs(deviation) < self.deviation_exit:
                self.sell(tick.last_price, abs(self.pos)) if self.pos > 0 else self.cover(tick.last_price, abs(self.pos))
                self.write_log(f"VWAP平仓: 回归{deviation*100:.2f}%")

    def on_bar(self, bar: BarData):
        """1分钟K线近似模式（当Tick不可用时）"""
        if self.use_tick_mode:
            return

        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # 用典型价格近似VWAP
        typical_price = (bar.high_price + bar.low_price + bar.close_price) / 3
        self.cum_volume += bar.volume
        self.cum_pv += typical_price * bar.volume
        self.vwap = self.cum_pv / self.cum_volume if self.cum_volume > 0 else typical_price

        deviation = (bar.close_price - self.vwap) / self.vwap

        if self.pos == 0 and self.daily_trades < self.max_daily_trades:
            if deviation < -self.deviation_entry:
                self.buy(bar.close_price, self.fixed_size)
                self.daily_trades += 1
            elif deviation > self.deviation_entry:
                self.short(bar.close_price, self.fixed_size)
                self.daily_trades += 1
        elif self.pos != 0:
            if abs(deviation) < self.deviation_exit:
                self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))

    def on_new_day(self):
        """每日重置"""
        self.cum_volume = 0
        self.cum_pv = 0.0
        self.vwap = 0.0
        self.tick_count = 0
        self.daily_trades = 0
        self.write_log("VWAP策略: 新交易日重置")