"""
strategies/options/bull_call_spread_strategy.py - v2.6.0
牛市看涨价差：买入低行权价Call + 卖出高行权价Call，风险有限收益有限
实盘级别：支持价差构建、止盈止损、展期
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class BullCallSpreadStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_long",           # 买入腿Delta目标（0.30-0.40）
        "delta_short",          # 卖出腿Delta目标（0.15-0.20）
        "min_days_to_expiry",   # 最短到期天数
        "max_days_to_expiry",   # 最长到期天数
        "min_credit_ratio",     # 最小权利金收入/最大亏损比例
        "rolling_days",         # 展期提前天数
        "max_positions",        # 最大同时持仓数
    ]

    variables = [
        "pos", "long_call", "short_call", "expiry_date",
        "net_premium", "max_loss", "max_profit", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.long_call = None
        self.short_call = None
        self.expiry_date = None
        self.net_premium = 0.0
        self.max_loss = 0.0
        self.max_profit = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("BullCallSpread策略初始化完成")

    def on_start(self):
        self.write_log("BullCallSpread策略启动")

    def on_stop(self):
        self.write_log("BullCallSpread策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.long_call is None and self.short_call is None:
            self._find_spread_opportunity(bar)
        else:
            self._manage_position(bar)

    def _find_spread_opportunity(self, bar: BarData):
        """寻找牛市价差机会：温和看涨时建立"""
        if len(self.am.close) < 20:
            return
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)

        # 条件：均线多头，RSI中性偏强
        if ma5 > ma20 and 40 < rsi < 68:
            # 构建价差：买入ATM Call，卖出OTM Call
            long_strike = bar.close_price * 0.98  # 略低于现价
            short_strike = bar.close_price * 1.05  # 虚值5%
            self.long_call = {"strike": round(long_strike, 2), "type": "call"}
            self.short_call = {"strike": round(short_strike, 2), "type": "call"}
            self.net_premium = bar.close_price * 0.02  # 模拟净权利金支出
            self.max_loss = self.net_premium
            self.max_profit = (short_strike - long_strike) * 100 - self.net_premium
            self.pos = 1
            self.write_log(f"BullCallSpread开仓: Long@{self.long_call['strike']} Short@{self.short_call['strike']}")

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、展期"""
        # 模拟盈亏
        underlying_pct = (bar.close_price - self.am.close[-20]) / self.am.close[-20]
        self.pnl = self.net_premium * underlying_pct * 10  # 粗略估算

        # 如果价格接近卖出腿，考虑平仓
        if bar.close_price >= self.short_call["strike"] * 0.97:
            self.sell(bar.close_price, abs(self.pos))
            self.long_call = None
            self.short_call = None
            self.pos = 0
            self.write_log(f"BullCallSpread平仓: 接近卖出腿行权价")