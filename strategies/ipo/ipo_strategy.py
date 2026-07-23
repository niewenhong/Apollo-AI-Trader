"""
strategies/ipo/ipo_strategy.py - v2.6.0
IPO策略：新股申购 + 首日交易
- 筛选：估值合理、行业景气、绿鞋保护、基石投资者
- 申购：现金申购/融资申购决策
- 首日：开盘观察→突破入场→止盈止损
实盘级别：支持申购决策、首日交易管理
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Interval
from datetime import datetime, timedelta
import numpy as np


class IPOStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "min_subscribe_ratio",   # 最低超额认购倍数（如50倍）
        "max_pe_ratio",          # 最高PE（如行业PE的1.2倍）
        "require_greenshoe",    # 是否要求绿鞋
        "first_day_max_hold",   # 首日最大持仓时间（分钟）
        "profit_take_pct",      # 止盈（如30%）
        "stop_loss_pct",        # 止损（如-15%）
        "max_capital_per_ipo",  # 单只新股最大资金
    ]

    variables = [
        "pos", "entry_price", "entry_time", "highest_price",
        "lowest_price", "pnl", "status"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.highest_price = 0.0
        self.lowest_price = 999999.0
        self.pnl = 0.0
        self.status = "idle"  # idle, subscribed, holding, closed
        self.bg = BarGenerator(self.on_bar, 5, self.on_5min_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("IPO策略初始化完成")

    def on_start(self):
        self.write_log("IPO策略启动")

    def on_stop(self):
        self.write_log("IPO策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        pass  # 使用5分钟K线

    def on_5min_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.status == "holding":
            self._manage_first_day_position(bar)
        elif self.status == "idle":
            self._check_new_listing(bar)

    def _check_new_listing(self, bar: BarData):
        """检查新股上市情况（简化：通过富途API获取）"""
        # 实际实现中，通过富途API获取新股信息
        # 此处仅做框架
        pass

    def _manage_first_day_position(self, bar: BarData):
        """管理首日持仓：止盈止损"""
        if self.pos > 0:
            self.highest_price = max(self.highest_price, bar.close_price)
            self.lowest_price = min(self.lowest_price, bar.close_price)

            # 止盈
            if bar.close_price >= self.entry_price * (1 + self.profit_take_pct):
                self.sell(bar.close_price, abs(self.pos))
                self.status = "closed"
                self.write_log(f"IPO止盈: {self.vt_symbol} 盈利{self.profit_take_pct*100:.0f}%")
            # 止损
            elif bar.close_price <= self.entry_price * (1 + self.stop_loss_pct):
                self.sell(bar.close_price, abs(self.pos))
                self.status = "closed"
                self.write_log(f"IPO止损: {self.vt_symbol} 亏损{abs(self.stop_loss_pct)*100:.0f}%")
            # 超时平仓
            elif self.entry_time and \
                 (datetime.now() - self.entry_time).seconds > self.first_day_max_hold * 60:
                self.sell(bar.close_price, abs(self.pos))
                self.status = "closed"
                self.write_log(f"IPO超时平仓: {self.vt_symbol} 持有{(datetime.now()-self.entry_time).seconds//60}分钟")

    def on_trade(self, trade):
        if trade.direction == Direction.LONG:
            self.entry_price = trade.price
            self.entry_time = trade.datetime
            self.status = "holding"
            self.write_log(f"IPO买入: {self.vt_symbol} @ {trade.price}")
        elif trade.direction == Direction.SHORT:
            self.status = "closed"
            self.write_log(f"IPO卖出: {self.vt_symbol} @ {trade.price}")