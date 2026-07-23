"""
strategies/equity/grid_strategy.py - Apollo-AI-Trader v2.6.0
网格策略：震荡市均值回归，在价格通道内低买高卖
⚠️ 趋势市会爆仓，仅用于确认震荡的标的
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData
import numpy as np


class GridStrategy(CtaTemplate):
    author = "Apollo"
    version = "v2.6.0"

    grid_levels = 5           # 网格层数
    grid_spacing_pct = 0.02   # 网格间距百分比
    lookback = 20             # 中枢计算回看
    fixed_size = 50
    max_position = 500         # 最大持仓
    stop_loss_pct = 0.08      # 中枢突破止损

    parameters = ["grid_levels","grid_spacing_pct","lookback","fixed_size","max_position","stop_loss_pct"]
    variables = ["pos","center_price","grid_size"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0; self.center_price = 0.0; self.grid_size = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self): self.load_bar(10, use_database=True)
    def on_start(self): self.write_log(f"[Grid] start {self.vt_symbol}")
    def on_stop(self): self.write_log(f"[Grid] stop")
    def on_tick(self, t): self.bg.update_tick(t)

    def on_bar(self, bar: BarData):
        am = self.am; am.update_bar(bar)
        if not am.inited: return
        # 计算中枢
        recent = am.close[-self.lookback:]
        self.center_price = np.mean(recent)
        self.grid_size = self.center_price * self.grid_spacing_pct
        # 止损：突破中枢太远
        deviation = (bar.close_price - self.center_price) / self.center_price
        if abs(deviation) > self.stop_loss_pct:
            if self.pos > 0: self.sell(bar.close_price-0.01, abs(self.pos))
            if self.pos < 0: self.cover(bar.close_price+0.01, abs(self.pos))
            return
        # 网格买卖
        if self.pos < self.max_position:
            for i in range(1, self.grid_levels+1):
                buy_price = self.center_price - i * self.grid_size
                if bar.close_price <= buy_price:
                    self.buy(buy_price+0.01, self.fixed_size)
                    break
        if self.pos > -self.max_position:
            for i in range(1, self.grid_levels+1):
                sell_price = self.center_price + i * self.grid_size
                if bar.close_price >= sell_price:
                    self.short(sell_price-0.01, self.fixed_size)
                    break
        # 回到中枢附近 → 平仓
        if abs(deviation) < self.grid_spacing_pct * 0.5:
            if self.pos > 0: self.sell(bar.close_price-0.01, abs(self.pos))
            if self.pos < 0: self.cover(bar.close_price+0.01, abs(self.pos))

    def on_order(self, order):
        if order.traded > 0:
            self.pos += order.traded if order.direction.name=="LONG" else -order.traded
        self.put_event()
