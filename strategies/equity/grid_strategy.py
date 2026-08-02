"""
strategies/equity/grid_strategy.py - v3.1.4
网格交易策略 + ATR动态间距 + 趋势感知 + Regime过滤
继承 BaseStrategy，启动交易保护
"""
from vnpy.trader.object import BarData, TradeData

from strategies.base_strategy import BaseStrategy


class GridStrategy(BaseStrategy):
    """网格交易策略（v3.1.4）"""

    author = "Apollo"

    parameters = BaseStrategy.parameters + [
        "grid_count",
        "grid_spacing_pct",
        "center_price",
        "use_atr_spacing",
        "atr_spacing_multiplier",
        "recenter_threshold_pct",
        "trend_filter_strength",
        "atr_period",
    ]
    variables = BaseStrategy.variables + [
        "grid_upper", "grid_lower",
        "last_grid_level", "atr_val",
        "_5m_ma_diff",
    ]

    DEFAULTS = {
        **BaseStrategy.DEFAULTS,
        "grid_count": 5,
        "grid_spacing_pct": 0.01,
        "center_price": 0.0,
        "use_atr_spacing": True,
        "atr_spacing_multiplier": 1.0,
        "recenter_threshold_pct": 2.0,
        "trend_filter_strength": 0.5,
        "atr_period": 14,
        "max_holding_bars": 120,
    }

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.grid_upper = 0.0
        self.grid_lower = 0.0
        self.last_grid_level = 0
        self.atr_val = 0.0
        self._5m_ma_diff = 0.0

        self._grid_levels = []
        self._init_done = False

        from vnpy.trader.utility import ArrayManager
        self.am_5m = ArrayManager(100)

    def on_init(self):
        super().on_init()
        self.write_log(f"网格策略初始化 | 层数={self.grid_count} ATR间距={self.use_atr_spacing}")

    # ── 1M 层：网格执行 ──
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        if not self.am.inited:
            return

        close = bar.close_price
        self.atr_val = self.am.atr(self.atr_period, array=False)

        # 初始化网格
        if not self._init_done:
            center = self.center_price if self.center_price > 0 else close
            self._build_grid(center, close)
            self._init_done = True
            return

        # 中心漂移
        center = getattr(self, '_5m_center', None)
        if center is None:
            center = self.grid_lower + (self.grid_upper - self.grid_lower) / 2

        drift_pct = abs(close - center) / center * 100 if center > 0 else 0
        if drift_pct > self.recenter_threshold_pct:
            self._build_grid(close, close)
            self.write_log(f"📊 网格重建(漂移{drift_pct:.1f}%) | 新中心={close:.2f}")

        # 穿越检测
        current_level = self._price_to_level(close)
        if current_level != self.last_grid_level and self.last_grid_level != 0:
            direction = "UP" if current_level > self.last_grid_level else "DOWN"
            levels_crossed = abs(current_level - self.last_grid_level)
            self.write_log(f"📊 网格穿越: {direction} 层级={current_level} 跨越={levels_crossed}")

            trend_ok = True
            if self.trend_filter_strength > 0 and self._5m_ma_diff != 0:
                if direction == "UP" and self._5m_ma_diff < -self.trend_filter_strength:
                    trend_ok = False
                elif direction == "DOWN" and self._5m_ma_diff > self.trend_filter_strength:
                    trend_ok = False

            if trend_ok and getattr(self, '_trading_allowed', False):
                if direction == "UP" and self.pos <= 0:
                    if self.pos < 0:
                        self.cover(close, abs(self.pos))
                    self.buy(close, self.fixed_size * levels_crossed)
                    self.write_log(f"🟢 网格买 | {close:.2f}")
                elif direction == "DOWN" and self.pos >= 0:
                    if self.pos > 0:
                        self.sell(close, abs(self.pos))
                    self.short(close, self.fixed_size * levels_crossed)
                    self.write_log(f"🔴 网格卖 | {close:.2f}")

        self.last_grid_level = current_level

        # 超时回收
        if self.pos != 0 and self.bars_held >= self.max_holding_bars:
            if self.pos > 0:
                self.sell(close, abs(self.pos))
            else:
                self.cover(close, abs(self.pos))
            self.write_log(f"⏰ 网格超时回收 | bars={self.bars_held}")

    # ── 5M 层 ──
    def on_5m_bar(self, bar: BarData):
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        ma_fast = self.am_5m.sma(10, array=False)
        ma_slow = self.am_5m.sma(30, array=False)
        self._5m_ma_diff = (ma_fast - ma_slow) / ma_slow * 100 if ma_slow > 0 else 0
        self._5m_center = ma_fast

    # ── 网格构建 ──
    def _build_grid(self, center: float, current_price: float):
        if self.use_atr_spacing and self.atr_val > 0:
            spacing = self.atr_val * self.atr_spacing_multiplier
        else:
            spacing = center * self.grid_spacing_pct
        spacing = max(spacing, center * 0.003)

        self._grid_levels = []
        for i in range(-self.grid_count, self.grid_count + 1):
            self._grid_levels.append(round(center + i * spacing, 2))

        self.grid_upper = self._grid_levels[-1]
        self.grid_lower = self._grid_levels[0]
        self.last_grid_level = self._price_to_level(current_price)
        self.write_log(f"📊 网格 | 中心={center:.2f} 间距={spacing:.2f} [{self.grid_lower:.2f}, {self.grid_upper:.2f}]")

    def _price_to_level(self, price: float) -> int:
        if not self._grid_levels:
            return 0
        result = -1
        for i, lp in enumerate(self._grid_levels):
            if price >= lp:
                result = i
            else:
                break
        return result

    def on_trade(self, trade: TradeData):
        super().on_trade(trade)
        self.write_log(f"💰 网格成交: {trade.direction.name} {trade.volume}@{trade.price:.2f}")
