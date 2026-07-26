"""
strategies/equity/grid_strategy.py - v2.8.0
网格交易策略：震荡市高抛低吸
v2.8.0 优化：继承 ApolloBaseStrategy，统一接口规范
"""
from typing import Optional
import numpy as np

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction

from strategies.base_strategy import ApolloBaseStrategy


class GridStrategy(ApolloBaseStrategy):
    """网格交易策略"""

    author = "Apollo"

    parameters = ApolloBaseStrategy.parameters + [
        "grid_count",           # 网格层数
        "grid_spacing_pct",     # 每层间距（百分比）
        "center_price",         # 网格中心价（0=自动使用当前价）
        "use_atr_spacing",      # 是否用ATR动态调整间距
        "atr_spacing_multiplier", # ATR倍数作为间距
    ]
    variables = ApolloBaseStrategy.variables + [
        "grid_upper", "grid_lower",
        "last_grid_level",
    ]

    DEFAULTS = {
        **ApolloBaseStrategy.DEFAULTS,
        "grid_count": 5,
        "grid_spacing_pct": 0.01,
        "center_price": 0.0,
        "use_atr_spacing": True,
        "atr_spacing_multiplier": 1.0,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.grid_upper = 0.0
        self.grid_lower = 0.0
        self.last_grid_level = 0
        self._grid_levels: list = []
        self._init_done = False

    def on_init(self):
        super().on_init()
        self.write_log(f"网格策略初始化 | 层数={self.grid_count} 间距={self.grid_spacing_pct*100:.1f}%")

    def on_bar(self, bar: BarData):
        """Bar回调：计算网格并交易"""
        close = bar.close_price

        # 首次初始化网格
        if not self._init_done:
            center = self.center_price if self.center_price > 0 else close
            self._build_grid(center, close)
            self._init_done = True
            return

        # 动态更新网格（中心跟随价格中值）
        if close > self.grid_upper * 1.02 or close < self.grid_lower * 0.98:
            center = close
            self._build_grid(center, close)

        # 判断当前网格层级
        current_level = self._price_to_level(close)

        # 穿越网格 → 交易
        if current_level != self.last_grid_level and self.last_grid_level != 0:
            direction = "UP" if current_level > self.last_grid_level else "DOWN"
            levels_crossed = abs(current_level - self.last_grid_level)

            self.write_log(f"📊 网格穿越: {direction} 层级={current_level} 跨越={levels_crossed}")

            if direction == "UP" and self.pos <= 0:
                # 价格上行 → 平空 + 开多
                if self.pos < 0:
                    self.cover(close, abs(self.pos))
                self.buy(close, self.fixed_size * levels_crossed)
            elif direction == "DOWN" and self.pos >= 0:
                # 价格下行 → 平多 + 开空
                if self.pos > 0:
                    self.sell(close, abs(self.pos))
                if hasattr(self, 'use_short') and self.use_short:
                    self.short(close, self.fixed_size * levels_crossed)

        self.last_grid_level = current_level

    def on_tick(self, tick: TickData):
        """Tick回调直接转Bar处理"""
        pass  # 网格策略用Bar即可

    def _build_grid(self, center: float, current_price: float):
        """构建网格层级"""
        if self.use_atr_spacing and hasattr(self, 'atr_val'):
            spacing = self.atr_val * self.atr_spacing_multiplier
        else:
            spacing = center * self.grid_spacing_pct

        spacing = max(spacing, center * 0.003)  # 最小间距 0.3%

        self._grid_levels = []
        for i in range(-self.grid_count, self.grid_count + 1):
            self._grid_levels.append(round(center + i * spacing, 2))

        self.grid_upper = self._grid_levels[-1]
        self.grid_lower = self._grid_levels[0]
        self.last_grid_level = self._price_to_level(current_price)

        self.write_log(
            f"📊 网格重建 | 中心={center:.2f} 间距={spacing:.2f} "
            f"范围=[{self.grid_lower:.2f}, {self.grid_upper:.2f}]"
        )

    def _price_to_level(self, price: float) -> int:
        """将价格映射到网格层级索引"""
        if not self._grid_levels:
            return 0
        for i, level_price in enumerate(self._grid_levels):
            if price >= level_price:
                return i
        return 0

    def on_trade(self, trade):
        super().on_trade(trade)
        self.write_log(f"💰 网格成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
