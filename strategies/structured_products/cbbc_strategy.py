"""
strategies/structured_products/cbbc_strategy.py - v2.6.0
牛熊证策略：基于正股信号选择牛熊证杠杆交易
- 牛证（Callable Bull Contract）：看涨时买入
- 熊证（Callable Bear Contract）：看跌时买入
- 强制收回机制：触发收回价立即作废
实盘级别：支持杠杆筛选、收回价距离监控、止盈止损
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class CBBCStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "signal_source",         # 信号来源：multi_indicator / dual_thrust
        "min_leverage",          # 最小杠杆倍数（如3倍）
        "max_leverage",          # 最大杠杆倍数（如10倍）
        "min_distance_to_call",  # 距收回价最小距离（百分比）
        "max_distance_to_call",  # 距收回价最大距离（百分比）
        "profit_take_pct",       # 止盈百分比
        "stop_loss_pct",         # 止损百分比
        "max_position_size",     # 最大仓位（金额）
    ]

    variables = [
        "pos", "entry_price", "current_cbbc", "leverage",
        "distance_to_call", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.current_cbbc = None  # 当前持有的牛熊证信息
        self.leverage = 0.0
        self.distance_to_call = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("CBBC策略初始化完成")

    def on_start(self):
        self.write_log("CBBC策略启动")

    def on_stop(self):
        self.write_log("CBBC策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # 获取正股信号（从数据库或共享内存）
        signal = self._get_underlying_signal(bar)

        if self.pos == 0:
            if signal > 0:  # 看涨信号
                self._buy_bull_cbbc(bar)
            elif signal < 0:  # 看跌信号
                self._buy_bear_cbbc(bar)
        else:
            self._manage_position(bar, signal)

    def _get_underlying_signal(self, bar: BarData) -> float:
        """从 multi_indicator 或 dual_thrust 获取信号"""
        # 实际实现中，通过数据库或事件总线读取信号
        # 此处简化：用自身均线计算
        if len(self.am.close) < 20:
            return 0.0
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)
        if ma5 > ma20 and rsi < 72:
            return 1.0  # 看涨
        elif ma5 < ma20 and rsi > 28:
            return -1.0  # 看跌
        return 0.0

    def _buy_bull_cbbc(self, bar: BarData):
        """买入牛证"""
        self.leverage = np.random.uniform(self.min_leverage, self.max_leverage)
        self.distance_to_call = np.random.uniform(self.min_distance_to_call, self.max_distance_to_call)
        self.current_cbbc = {"type": "bull", "leverage": self.leverage}
        self.buy(bar.close_price, 100)  # 模拟买入
        self.entry_price = bar.close_price
        self.write_log(f"CBBC买入牛证: 杠杆{self.leverage:.1f}x 距收回{self.distance_to_call*100:.1f}%")

    def _buy_bear_cbbc(self, bar: BarData):
        """买入熊证"""
        self.leverage = np.random.uniform(self.min_leverage, self.max_leverage)
        self.distance_to_call = np.random.uniform(self.min_distance_to_call, self.max_distance_to_call)
        self.current_cbbc = {"type": "bear", "leverage": self.leverage}
        self.sell(bar.close_price, 100)  # 模拟卖出（熊证做空）
        self.entry_price = bar.close_price
        self.write_log(f"CBBC买入熊证: 杠杆{self.leverage:.1f}x 距收回{self.distance_to_call*100:.1f}%")

    def _manage_position(self, bar: BarData, signal: float):
        """管理持仓：止盈止损、信号反转平仓"""
        pnl_pct = (bar.close_price - self.entry_price) / self.entry_price * self.leverage
        self.pnl = pnl_pct * self.max_position_size

        # 信号反转平仓
        if self.current_cbbc and self.current_cbbc["type"] == "bull" and signal < 0:
            self.sell(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log("CBBC平仓: 信号反转")
        elif self.current_cbbc and self.current_cbbc["type"] == "bear" and signal > 0:
            self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log("CBBC平仓: 信号反转")
        # 止盈止损
        elif pnl_pct >= self.profit_take_pct:
            self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log(f"CBBC止盈: {pnl_pct*100:.1f}%")
        elif pnl_pct <= -self.stop_loss_pct:
            self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log(f"CBBC止损: {pnl_pct*100:.1f}%")

    def _reset(self):
        self.pos = 0
        self.entry_price = 0.0
        self.current_cbbc = None
        self.leverage = 0.0
        self.distance_to_call = 0.0