"""
strategies/equity/trend_strategy.py - EMA趋势跟踪策略
v3.1.4：继承 BaseStrategy，启动交易保护
"""
from strategies.base_strategy import BaseStrategy
from vnpy.trader.object import BarData
from vnpy.trader.utility import ArrayManager
import logging

logger = logging.getLogger("TrendStrategy")


class TrendStrategy(BaseStrategy):
    """EMA趋势跟踪策略（5分钟K线）"""

    author = "Apollo Team"

    # 参数
    ema_fast = 12
    ema_slow = 52
    trading_hours_start = "09:30"
    trading_hours_end = "16:00"
    fixed_size = 100

    # 变量
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
        self.am_5m = ArrayManager(size=100)
        self.last_signal = 0
        self.pos = 0
        self._initial_check_done = False

        # 解析交易时段
        self._trading_start = self._parse_time(self.trading_hours_start)
        self._trading_end = self._parse_time(self.trading_hours_end)

        # 3秒后备定时器
        self._timer_count = 0
        self._timer_target = 3

    def on_init(self):
        """初始化——交易锁定"""
        self.write_log("TrendStrategy 初始化")
        super().on_init()
        self._timer_count = 0

    def on_start(self):
        """启动——开放交易，重置首次检查"""
        self.write_log("TrendStrategy 启动")
        super().on_start()
        self._initial_check_done = False

    def on_timer(self):
        """定时器（每秒触发）"""
        self._timer_count += 1
        if self._timer_count == self._timer_target:
            self._try_open_from_last_bar()

    def on_5min_bar(self, bar: BarData):
        """5分钟K线回调"""
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
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
            self.write_log(f"[CHECK] 首次检查，trend={trend}, last_signal={self.last_signal}")
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

    def _open_with_builtin(self, trend, price):
        """使用基类的buy/short（自动受_trading_allowed保护）"""
        if trend == 1:
            self.buy(price, self.fixed_size)
            self.last_signal = 1
        elif trend == -1:
            self.short(price, self.fixed_size)
            self.last_signal = -1

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
            self.write_log(f"[TIMER] 后备开仓，trend={trend}, price={last_close}")
            self._open_with_builtin(trend, last_close)

    @staticmethod
    def _parse_time(time_str: str):
        from datetime import time
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))
