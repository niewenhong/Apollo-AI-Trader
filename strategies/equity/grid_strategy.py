"""
strategies/equity/grid_strategy.py - v2.6.0
网格交易策略：在震荡市中通过预设价格网格低买高卖
实验状态：适用于横盘整理的市场
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class GridStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "grid_layers",        # 网格层数
        "grid_spacing_pct",   # 网格间距百分比
        "initial_position",   # 初始仓位（正值表示多头）
        "take_profit_pct",    # 止盈百分比
        "stop_loss_pct",      # 止损百分比
        "max_position",
    ]

    variables = [
        "pos", "grid_levels", "current_layer",
        "avg_cost", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.grid_levels = []  # 每个网格层的价格
        self.current_layer = 0
        self.avg_cost = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("Grid策略初始化完成")

    def on_start(self):
        self.write_log("Grid策略启动")

    def on_stop(self):
        self.write_log("Grid策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # 初始化网格
        if not self.grid_levels:
            self._init_grid(bar.close_price)

        # 检查每个网格层
        for layer, price in enumerate(self.grid_levels):
            if self.pos < self.max_position:
                # 价格跌到网格买入层
                if bar.low_price <= price and self.current_layer <= layer:
                    self.buy(price, 1)
                    self.current_layer = layer + 1
                    self.write_log(f"网格买入: 层{layer} @ {price:.2f}")
            # 价格涨到网格卖出层
            if self.pos > 0 and bar.high_price >= price and self.current_layer > layer:
                self.sell(price, 1)
                self.current_layer = layer - 1
                self.write_log(f"网格卖出: 层{layer} @ {price:.2f}")

        # 止损
        if self.pos > 0:
            if bar.close_price < self.avg_cost * (1 - self.stop_loss_pct):
                self.sell(bar.close_price, abs(self.pos))
                self.write_log("网格止损")

    def _init_grid(self, current_price: float):
        """初始化网格价格水平"""
        self.grid_levels = []
        for i in range(self.grid_layers):
            level = current_price * (1 + (i - self.grid_layers // 2) * self.grid_spacing_pct)
            self.grid_levels.append(round(level, 2))
        self.grid_levels.sort()
        self.current_layer = self.grid_layers // 2
        self.write_log(f"网格初始化: {len(self.grid_levels)}层, 中间层@{current_price:.2f}")

    def on_trade(self, trade):
        # 更新平均成本
        if trade.direction == Direction.LONG:
            self.avg_cost = (self.avg_cost * (self.pos - trade.volume) + trade.price * trade.volume) / self.pos if self.pos > 0 else trade.price
        self.write_log(f"网格成交: {trade.direction.name} {trade.volume}手 @ {trade.price}")