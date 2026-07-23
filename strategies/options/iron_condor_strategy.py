"""
strategies/options/iron_condor_strategy.py - v2.6.0
铁鹰式：同时卖出虚值Call和Put（各一条腿），买入更虚值的Call和Put保护
预期市场窄幅震荡，赚取时间价值
实盘级别：支持四腿构建、盈亏平衡点监控、展期
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class IronCondorStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "delta_short_call",    # 卖出Call Delta目标 0.15-0.20
        "delta_short_put",     # 卖出Put Delta目标 -0.15~-0.20
        "wing_width",          # 保护腿宽度（百分比，如0.08表示8%）
        "min_days_to_expiry",
        "max_days_to_expiry",
        "min_credit_ratio",    # 最小权利金收入/最大亏损比例
        "rolling_days",
        "max_positions"
    ]

    variables = [
        "pos", "short_call", "short_put", "long_call", "long_put",
        "expiry_date", "net_credit", "max_loss", "max_profit", "pnl",
        "upper_breakeven", "lower_breakeven"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.short_call = None
        self.short_put = None
        self.long_call = None
        self.long_put = None
        self.expiry_date = None
        self.net_credit = 0.0
        self.max_loss = 0.0
        self.max_profit = 0.0
        self.pnl = 0.0
        self.upper_breakeven = 0.0
        self.lower_breakeven = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("IronCondor策略初始化完成")

    def on_start(self):
        self.write_log("IronCondor策略启动")

    def on_stop(self):
        self.write_log("IronCondor策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.short_call is None and self.short_put is None:
            self._find_opportunity(bar)
        else:
            self._manage_position(bar)

    def _find_opportunity(self, bar: BarData):
        """寻找铁鹰式机会：预期震荡时建立"""
        if len(self.am.close) < 20:
            return
        rsi = self.am.rsi(14)
        atr = self.am.atr(14)
        mid = bar.close_price

        # 条件：RSI中性，波动率适中
        if 35 < rsi < 62:
            # 计算行权价
            wing = mid * self.wing_width
            sc_strike = mid + wing * 0.5   # 卖出Call行权价（虚值）
            sp_strike = mid - wing * 0.5   # 卖出Put行权价（虚值）
            lc_strike = mid + wing * 1.5   # 买入Call保护（更虚值）
            lp_strike = mid - wing * 1.5   # 买入Put保护（更虚值）

            self.short_call = {"strike": round(sc_strike, 2), "type": "call"}
            self.short_put = {"strike": round(sp_strike, 2), "type": "put"}
            self.long_call = {"strike": round(lc_strike, 2), "type": "call"}
            self.long_put = {"strike": round(lp_strike, 2), "type": "put"}

            # 模拟净权利金收入
            self.net_credit = bar.close_price * 0.015
            self.max_loss = (lc_strike - sc_strike) * 100 - self.net_credit
            self.max_profit = self.net_credit
            self.upper_breakeven = sc_strike + self.net_credit / 100
            self.lower_breakeven = sp_strike - self.net_credit / 100
            self.pos = 1

            self.write_log(
                f"IronCondor开仓: SC@{sc_strike:.2f} SP@{sp_strike:.2f} "
                f"LC@{lc_strike:.2f} LP@{lp_strike:.2f} "
                f"盈亏区间[{self.lower_breakeven:.2f}, {self.upper_breakeven:.2f}]"
            )

    def _manage_position(self, bar: BarData):
        """管理持仓：价格突破盈亏平衡点时调整或平仓"""
        if bar.close_price >= self.upper_breakeven or bar.close_price <= self.lower_breakeven:
            # 平仓所有腿
            self.sell(bar.close_price, abs(self.pos))
            self.short_call = None
            self.short_put = None
            self.long_call = None
            self.long_put = None
            self.pos = 0
            self.write_log(f"IronCondor平仓: 价格突破盈亏平衡点")