"""
strategies/equity/trend_strategy.py - v3.8.0
EMA趋势跟踪策略

v3.8.0 变更：
- 继承 BaseStrategy（已集成 LifecycleManager）
- on_5m_bar 正确接收 BarGenerator 回调
- 使用 buy/short 自动获得日志 + 生命周期检查
- 支持多用户（user_id 从参数注入）
"""
import logging
from datetime import time as dtime, datetime
from vnpy.trader.object import BarData
from vnpy.trader.utility import ArrayManager

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("TrendStrategy")


class TrendStrategy(BaseStrategy):
    """EMA趋势跟踪策略（5分钟K线）"""

    author = "Apollo Team"

    # ─── 参数 ───
    ema_fast = 12
    ema_slow = 52
    trading_hours_start = "09:30"
    trading_hours_end = "16:00"
    fixed_size = 100

    # ─── 状态变量 ───
    last_signal = 0
    pos = 0
    _initial_check_done = False

    parameters = BaseStrategy.parameters + [
        "ema_fast", "ema_slow",
        "trading_hours_start", "trading_hours_end",
    ]
    variables = BaseStrategy.variables + [
        "last_signal", "pos",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 5分钟 ArrayManager
        self.am_5m = ArrayManager(size=100)
        self.last_signal = 0
        self.pos = 0
        self._initial_check_done = False

        # 解析交易时段
        self._trading_start = self._parse_time(self.trading_hours_start)
        self._trading_end = self._parse_time(self.trading_hours_end)

        # 后备定时器
        self._timer_count = 0
        self._timer_target = 3

        self.write_log(
            f"[Trend] 初始化: fast={self.ema_fast} slow={self.ema_slow} "
            f"user={getattr(self, 'user_id', 'SYSTEM')}"
        )

    # ────────────────────────────
    #  生命周期
    # ────────────────────────────
    def on_init(self):
        self.write_log("TrendStrategy 初始化")
        super().on_init()
        self._timer_count = 0

    def on_start(self):
        self.write_log("TrendStrategy 启动")
        super().on_start()
        self._initial_check_done = False

    def on_timer(self):
        """定时器（每秒触发）—— 后备开仓"""
        self._timer_count += 1
        if self._timer_count == self._timer_target:
            self._try_open_from_last_bar()

    # ────────────────────────────
    #  5分钟K线回调（核心信号）
    # ────────────────────────────
    def on_5m_bar(self, bar: BarData):
        """5分钟K线回调（由基类 BarGenerator 自动聚合）"""

        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            self.write_log(
                f"[5m] {bar.datetime.strftime('%H:%M')} "
                f"O={bar.open_price:.2f} H={bar.high_price:.2f} "
                f"L={bar.low_price:.2f} C={bar.close_price:.2f} "
                f"V={bar.volume} (等待初始化...)"
            )
            return

        # 计算EMA差值
        fast = self.am_5m.ema(self.ema_fast, array=False)
        slow = self.am_5m.ema(self.ema_slow, array=False)
        diff = fast - slow

        if diff > 0.001:
            trend = 1
        elif diff < -0.001:
            trend = -1
        else:
            trend = 0

        self.write_log(
            f"[5m] {bar.datetime.strftime('%H:%M')} "
            f"C={bar.close_price:.2f} fast={fast:.2f} slow={slow:.2f} "
            f"diff={diff:.4f} trend={trend}"
        )

        # 首次检查（实盘第一根K线）
        if not self._initial_check_done:
            self._initial_check_done = True
            self.write_log(f"[CHECK] 首次检查 trend={trend} last_signal={self.last_signal}")
            if trend != 0 and trend != self.last_signal:
                self._open_with_builtin(trend, bar.close_price)
            return

        # 有持仓不开新仓
        if self.pos != 0:
            return
        if trend == self.last_signal or trend == 0:
            return

        # 检查交易时间
        t = bar.datetime.time()
        if not (self._trading_start <= t <= self._trading_end):
            return

        self._open_with_builtin(trend, bar.close_price)

    # ────────────────────────────
    #  开仓方法
    # ────────────────────────────
    def _open_with_builtin(self, trend: int, price: float):
        """使用基类的 buy/short（自动获得日志 + LifecycleManager 检查）"""
        if trend == 1:
            self.buy(price, self.fixed_size)
            self.last_signal = 1
            self.write_log(f"📈 买入信号: {self.fixed_size}@{price:.2f}")
        elif trend == -1:
            self.short(price, self.fixed_size)
            self.last_signal = -1
            self.write_log(f"📉 卖出信号: {self.fixed_size}@{price:.2f}")

    def _try_open_from_last_bar(self):
        """后备：3秒后无实盘K线时用最后一根K线开仓"""
        if self._initial_check_done:
            return
        if not self.am_5m.inited:
            return

        last_close = self.am_5m.close[-1] if self.am_5m.close.size > 0 else 0
        if last_close == 0:
            return

        fast = self.am_5m.ema(self.ema_fast, array=False)
        slow = self.am_5m.ema(self.ema_slow, array=False)
        diff = fast - slow

        if diff > 0.001:
            trend = 1
        elif diff < -0.001:
            trend = -1
        else:
            trend = 0

        if trend != 0 and trend != self.last_signal:
            self.write_log(f"[TIMER] 后备开仓 trend={trend} price={last_close:.2f}")
            self._open_with_builtin(trend, last_close)

    # ────────────────────────────
    #  工具方法
    # ────────────────────────────
    @staticmethod
    def _parse_time(time_str: str) -> dtime:
        parts = time_str.split(":")
        return dtime(int(parts[0]), int(parts[1]))
