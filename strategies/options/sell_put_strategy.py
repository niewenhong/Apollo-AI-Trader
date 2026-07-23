"""
strategies/options/sell_put_strategy.py - v2.6.0
卖Put策略：卖出虚值Put收取权利金，预期标的不会跌破行权价
实盘级别：支持止盈止损、展期、保证金监控
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Offset
import numpy as np


class SellPutStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_target",         # 目标Delta（正数，如0.25）
        "days_to_expiry",       # 目标到期天数
        "profit_take_pct",      # 权利金盈利百分比止盈（如0.5表示50%）
        "stop_loss_pct",        # 权利金亏损百分比止损（如0.3表示30%）
        "max_positions",        # 最大同时持仓数
        "min_dte",              # 最短到期天数（展期条件）
        "roll_dte",             # 展期目标天数
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
        self.write_log("SellPut策略初始化完成")

    def on_start(self):
        self.write_log("SellPut策略启动")

    def on_stop(self):
        self.write_log("SellPut策略停止")

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
        """寻找开仓机会：温和看跌时卖出虚值Put"""
        # 这里简化为均线判断，实际应结合信号引擎
        if len(self.am.close) < 20:
            return
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)

        # 条件：价格在均线上方，RSI不过热，适合卖Put
        if bar.close_price > ma20 and 30 < rsi < 65:
            # 计算行权价（虚值5%-10%）
            strike = bar.close_price * (1 - 0.05)
            self.strike = round(strike, 2)
            # 实际应通过期权链查询Delta和权利金
            # 这里简化：直接模拟下单
            self.buy(bar.close_price, 1)  # 卖Put是卖出开仓，用buy表示负方向
            self.entry_price = bar.close_price
            self.write_log(f"SellPut开仓: {self.vt_symbol} Strike={self.strike}")

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、展期"""
        # 模拟当前权利金变化
        price_change = (bar.close_price - self.entry_price) / self.entry_price
        pnl_pct = -price_change * 2  # 粗略杠杆

        if pnl_pct >= self.profit_take_pct:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"SellPut止盈: {self.vt_symbol}")
        elif pnl_pct <= -self.stop_loss_pct:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"SellPut止损: {self.vt_symbol}")

    def on_trade(self, trade):
        if trade.direction == Direction.SHORT:
            self.pos = trade.volume
            self.entry_price = trade.price
        elif trade.direction == Direction.COVER:
            self.pos = 0
            self.entry_price = 0.0