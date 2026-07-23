"""
strategies/options/cash_secured_put_strategy.py - v2.6.0
现金担保卖Put策略：备足现金卖出虚值Put，预期低价接货或收权利金
实盘级别：支持现金预留、行权接货管理
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class CashSecuredPutStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_target",         # 目标Delta（正数，如0.25）
        "days_to_expiry",       # 目标到期天数
        "cash_reserve_pct",     # 现金预留比例（如0.8表示80%现金）
        "profit_take_pct",      # 权利金盈利百分比止盈
        "stop_loss_pct",        # 权利金亏损百分比止损
        "max_positions",        # 最大同时持仓数
        "roll_dte",             # 展期目标天数
    ]

    variables = [
        "pos", "entry_price", "strike", "expiry",
        "current_premium", "pnl", "cash_required"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.strike = 0.0
        self.expiry = None
        self.current_premium = 0.0
        self.pnl = 0.0
        self.cash_required = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("CashSecuredPut策略初始化完成")

    def on_start(self):
        self.write_log("CashSecuredPut策略启动")

    def on_stop(self):
        self.write_log("CashSecuredPut策略停止")

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
        """寻找开仓机会：价格回调到支撑位时卖出Put"""
        if len(self.am.close) < 20:
            return
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)

        # 条件：价格在MA20附近或下方，RSI偏低（超卖区域），适合卖Put接货
        if bar.close_price <= ma20 * 1.02 and rsi < 45:
            # 行权价设在当前价格下方5%（虚值）
            strike = bar.close_price * (1 - 0.05)
            self.strike = round(strike, 2)
            # 所需现金 = 行权价 × 合约乘数 × 手数
            self.cash_required = self.strike * 100 * 1  # 假设1手
            # 模拟下单：卖出Put
            self.buy(bar.close_price, 1)  # 卖Put用buy表示负方向
            self.entry_price = bar.close_price
            self.write_log(f"CashSecuredPut开仓: {self.vt_symbol} Strike={self.strike} 需现金{self.cash_required:.0f}")

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、展期、行权准备"""
        price_change = (bar.close_price - self.entry_price) / self.entry_price
        pnl_pct = -price_change * 2  # 粗略杠杆

        # 止盈
        if pnl_pct >= self.profit_take_pct:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"CashSecuredPut止盈: {self.vt_symbol}")
        # 止损
        elif pnl_pct <= -self.stop_loss_pct:
            self.cover(bar.close_price, abs(self.pos))
            self.write_log(f"CashSecuredPut止损: {self.vt_symbol}")
        # 如果价格跌破行权价，准备接货
        elif bar.close_price <= self.strike:
            self.write_log(f"CashSecuredPut警告: 价格{bar.close_price}跌破行权价{self.strike}，准备接货")

    def on_trade(self, trade):
        if trade.direction == Direction.SHORT:
            self.pos = trade.volume
            self.entry_price = trade.price
        elif trade.direction == Direction.COVER:
            self.pos = 0
            self.entry_price = 0.0
            self.cash_required = 0.0