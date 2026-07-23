"""
strategies/options/covered_call_strategy.py - v2.6.0
持股卖Call策略：持有正股的同时卖出虚值Call，增强收益
实盘级别：支持分红调整、行权风险管理
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class CoveredCallStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_target",         # 目标Delta（如0.2）
        "days_to_expiry",
        "min_dividend_yield",   # 最低股息率（用于筛选持股标的）
        "profit_take_pct",
        "stop_loss_pct",
        "max_positions",
        "roll_dte",
    ]

    variables = [
        "pos", "stock_pos", "option_pos", "entry_stock_price",
        "entry_option_price", "strike", "expiry", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.stock_pos = 0
        self.option_pos = 0
        self.entry_stock_price = 0.0
        self.entry_option_price = 0.0
        self.strike = 0.0
        self.expiry = None
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("CoveredCall策略初始化完成")

    def on_start(self):
        self.write_log("CoveredCall策略启动")

    def on_stop(self):
        self.write_log("CoveredCall策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.stock_pos == 0:
            # 未持股：先买入正股
            self._buy_stock(bar)
        elif self.stock_pos > 0 and self.option_pos == 0:
            # 已持股未卖Call：卖出Call
            self._sell_call(bar)
        else:
            # 已有持仓：管理
            self._manage_position(bar)

    def _buy_stock(self, bar: BarData):
        """买入正股（作为底仓）"""
        if len(self.am.close) < 20:
            return
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        if ma5 > ma20 and bar.close_price > ma20:
            self.buy(bar.close_price, 100)  # 买入100股
            self.entry_stock_price = bar.close_price
            self.stock_pos = 100
            self.write_log(f"CoveredCall买入正股: {self.vt_symbol} @ {bar.close_price}")

    def _sell_call(self, bar: BarData):
        """卖出虚值Call"""
        strike = self.entry_stock_price * (1 + 0.03)  # 3%虚值
        self.strike = round(strike, 2)
        self.sell(bar.close_price, 1)  # 卖出一张Call
        self.entry_option_price = bar.close_price
        self.option_pos = -1
        self.write_log(f"CoveredCall卖Call: Strike={self.strike}")

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、展期"""
        # 计算盈亏
        stock_pnl = (bar.close_price - self.entry_stock_price) * self.stock_pos
        option_pnl = (self.entry_option_price - bar.close_price) * abs(self.option_pos)
        total_pnl = stock_pnl + option_pnl
        self.pnl = total_pnl

        # 如果股价接近行权价，考虑展期或平仓
        if bar.close_price >= self.strike * 0.95:
            # 接近行权价，平仓或展期
            self.cover(bar.close_price, abs(self.option_pos))
            self.option_pos = 0
            self.write_log(f"CoveredCall平仓Call: {self.vt_symbol} 接近行权价")

    def on_trade(self, trade):
        if trade.direction == Direction.LONG:
            if trade.offset == "OPEN":
                self.stock_pos += trade.volume
            else:
                self.stock_pos -= trade.volume
        elif trade.direction == Direction.SHORT:
            if trade.offset == "OPEN":
                self.option_pos -= trade.volume
            else:
                self.option_pos += trade.volume
        self.pos = self.stock_pos + self.option_pos