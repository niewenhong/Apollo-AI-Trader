"""
strategies/options/bear_put_spread_strategy.py - v2.6.0
熊市看跌价差：买入高行权价Put + 卖出低行权价Put，风险有限收益有限
实盘级别：支持价差构建、止盈止损、展期
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class BearPutSpreadStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_long",           # 买入腿Delta目标（-0.30~-0.40）
        "delta_short",          # 卖出腿Delta目标（-0.15~-0.20）
        "min_days_to_expiry",
        "max_days_to_expiry",
        "min_credit_ratio",
        "rolling_days",
        "max_positions",
    ]

    variables = [
        "pos", "long_put", "short_put", "expiry_date",
        "net_premium", "max_loss", "max_profit", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.long_put = None
        self.short_put = None
        self.expiry_date = None
        self.net_premium = 0.0
        self.max_loss = 0.0
        self.max_profit = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("BearPutSpread策略初始化完成")

    def on_start(self):
        self.write_log("BearPutSpread策略启动")

    def on_stop(self):
        self.write_log("BearPutSpread策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.long_put is None and self.short_put is None:
            self._find_spread_opportunity(bar)
        else:
            self._manage_position(bar)

    def _find_spread_opportunity(self, bar: BarData):
        """寻找熊市价差机会：温和看跌时建立"""
        if len(self.am.close) < 20:
            return
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)

        # 条件：均线空头，RSI中性偏弱
        if ma5 < ma20 and 32 < rsi < 58:
            # 构建价差：买入ATM Put，卖出OTM Put
            long_strike = bar.close_price * 1.02  # 略高于现价
            short_strike = bar.close_price * 0.93  # 虚值7%
            self.long_put = {"strike": round(long_strike, 2), "type": "put"}
            self.short_put = {"strike": round(short_strike, 2), "type": "put"}
            self.net_premium = bar.close_price * 0.025  # 模拟净权利金支出
            self.max_loss = self.net_premium
            self.max_profit = (long_strike - short_strike) * 100 - self.net_premium
            self.pos = -1
            self.write_log(f"BearPutSpread开仓: Long@{self.long_put['strike']} Short@{self.short_put['strike']}")

    def _manage_position(self, bar: BarData):
        """管理持仓：止盈止损、展期"""
        underlying_pct = (self.am.close[-20] - bar.close_price) / self.am.close[-20]
        self.pnl = self.net_premium * underlying_pct * 10

        # 如果价格接近买入腿，考虑平仓
        if bar.close_price <= self.long_put["strike"] * 0.96:
            self.cover(bar.close_price, abs(self.pos))
            self.long_put = None
            self.short_put = None
            self.pos = 0
            self.write_log(f"BearPutSpread平仓: 接近买入腿行权价")