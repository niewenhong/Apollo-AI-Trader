"""
strategies/options/straddle_strategy.py - v2.6.0
跨式策略：同时买入平值Call和Put，赌大波动（事件驱动）
注：高难度策略，默认实验状态，谨慎使用
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class StraddleStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "at_the_money_offset", # 平值附近偏移百分比
        "min_days_to_expiry",
        "max_days_to_expiry",
        "min_iv_percentile",   # 最低IV百分位（越低越好，波动率低时买入）
        "event_type",          # 事件类型：earnings, fed, etc.
        "profit_target",       # 盈利目标倍数（权利金的倍数）
        "stop_loss",           # 止损比例
        "max_positions"
    ]

    variables = [
        "pos", "call_leg", "put_leg", "expiry_date",
        "total_cost", "current_value", "pnl", "iv_percentile"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.call_leg = None
        self.put_leg = None
        self.expiry_date = None
        self.total_cost = 0.0
        self.current_value = 0.0
        self.pnl = 0.0
        self.iv_percentile = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("Straddle策略初始化完成")

    def on_start(self):
        self.write_log("Straddle策略启动")

    def on_stop(self):
        self.write_log("Straddle策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if self.call_leg is None and self.put_leg is None:
            self._find_opportunity(bar)
        else:
            self._manage_position(bar)

    def _find_opportunity(self, bar: BarData):
        """寻找跨式机会：重大事件前IV较低时建立"""
        if len(self.am.close) < 20:
            return
        rsi = self.am.rsi(14)
        atr = self.am.atr(14)
        # 模拟IV百分位（实际应从期权链获取）
        self.iv_percentile = np.random.uniform(20, 40)

        # 条件：RSI中性，IV处于低位，临近事件
        if 30 < rsi < 67 and self.iv_percentile < self.min_iv_percentile:
            atm_strike = bar.close_price * (1 + self.at_the_money_offset)
            self.call_leg = {"strike": round(atm_strike, 2), "type": "call"}
            self.put_leg = {"strike": round(atm_strike, 2), "type": "put"}
            # 模拟权利金成本
            self.total_cost = bar.close_price * 0.035 * 2  # 两条腿
            self.pos = 1
            self.write_log(
                f"Straddle开仓: Call@{atm_strike:.2f} Put@{atm_strike:.2f} "
                f"总成本{self.total_cost:.2f}"
            )

    def _manage_position(self, bar: BarData):
        """管理持仓：事件后平仓或止损"""
        # 模拟当前价值
        move = abs(bar.close_price - self.am.close[-20]) / self.am.close[-20]
        self.current_value = self.total_cost * (1 + move * 5)  # 粗略估算
        self.pnl = self.current_value - self.total_cost

        # 止盈
        if self.pnl >= self.total_cost * self.profit_target:
            self.sell(bar.close_price, abs(self.pos))
            self.call_leg = None
            self.put_leg = None
            self.pos = 0
            self.write_log(f"Straddle止盈: PnL={self.pnl:.2f}")
        # 止损
        elif self.pnl <= -self.total_cost * self.stop_loss:
            self.sell(bar.close_price, abs(self.pos))
            self.call_leg = None
            self.put_leg = None
            self.pos = 0
            self.write_log(f"Straddle止损: PnL={self.pnl:.2f}")