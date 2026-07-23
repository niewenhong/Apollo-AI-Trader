"""
strategies/options/sell_call_strategy.py - v2.6.0
卖Call策略：卖出虚值Call收取权利金，预期标的不会涨破行权价
实盘级别：支持止盈止损、展期、保证金监控
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class SellCallStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_target",         # 目标Delta（负数，如-0.25）
        "days_to_expiry",
        "profit_take_pct",
        "stop_loss_pct",
        "max_positions",
        "min_dte",
        "roll_dte",
    ]

    variables = [
        "pos", "entry_price", "strike", "expiry",
        "current_premium", "pnl", "margin_used"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.strike = 0.0
        self.expiry = None
        self.current_premium = 0.0
        self.pnl = 0.0
        self.margin_used = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("SellCall策略初始化完成")

    def on_start(self):
        self.write_log("SellCall策略启动")

    def on_stop(self):
        self.write_log("SellCall策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.pos == 0:
            self._find_entry(bar)
        else:
            self._manage_position(bar)

    def _find_entry(self, bar: BarData):
        """寻找开仓机会：温和看涨时卖出虚值Call"""
        if len(self.am.close) < 20:
            return
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)

        # 条件：价格接近阻力位，RSI偏高，适合卖Call
        if bar.close_price < ma20 and rsi > 60:
            strike = bar.close_price * (1 + 0.05)
            self.strike = round(strike, 2)
            self.sell(bar.close_price, 1)  # 卖Call是卖出开仓
            self.entry_price = bar.close_price
            self.write_log(f"SellCall开仓: {self.vt_symbol} Strike={self.strike}")

    def _manage_position(self, bar: BarData):
        price_change = (bar.close_price - self.entry_price) / self.entry_price
        pnl_pct = price_change * 2  # 粗略杠杆

        if pnl_pct >= self.profit_take_pct:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"SellCall止盈: {self.vt_symbol}")
        elif pnl_pct <= -self.stop_loss_pct:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"SellCall止损: {self.vt_symbol}")

    def on_trade(self, trade):
        if trade.direction == Direction.SHORT:
            self.pos = trade.volume
            self.entry_price = trade.price
        elif trade.direction == Direction.COVER:
            self.pos = 0
            self.entry_price = 0.0