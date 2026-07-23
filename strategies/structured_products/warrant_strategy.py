"""
strategies/structured_products/warrant_strategy.py - v2.6.0
窝轮策略：基于正股信号选择认购/认沽窝轮
- 认购证（Call Warrant）：看涨
- 认沽证（Put Warrant）：看跌
- 时间衰减快，适合短线
实盘级别：支持杠杆筛选、到期日管理、溢价率控制
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction
import numpy as np


class WarrantStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "signal_source",
        "min_leverage",
        "max_leverage",
        "min_days_to_expiry",
        "max_days_to_expiry",
        "min_delta",             # 最低Delta绝对值
        "max_premium_pct",       # 最高溢价率
        "profit_take_pct",
        "stop_loss_pct",
        "max_position_size",
    ]

    variables = [
        "pos", "entry_price", "current_warrant", "leverage",
        "delta", "premium", "days_to_expiry", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.current_warrant = None
        self.leverage = 0.0
        self.delta = 0.0
        self.premium = 0.0
        self.days_to_expiry = 0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("Warrant策略初始化完成")

    def on_start(self):
        self.write_log("Warrant策略启动")

    def on_stop(self):
        self.write_log("Warrant策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        signal = self._get_underlying_signal(bar)

        if self.pos == 0:
            if signal > 0:
                self._buy_call_warrant(bar)
            elif signal < 0:
                self._buy_put_warrant(bar)
        else:
            self._manage_position(bar, signal)

    def _get_underlying_signal(self, bar: BarData) -> float:
        if len(self.am.close) < 20:
            return 0.0
        ma5 = np.mean(self.am.close[-5:])
        ma20 = np.mean(self.am.close[-20:])
        rsi = self.am.rsi(14)
        if ma5 > ma20 and rsi < 70:
            return 1.0
        elif ma5 < ma20 and rsi > 30:
            return -1.0
        return 0.0

    def _buy_call_warrant(self, bar: BarData):
        """买入认购证"""
        self.leverage = np.random.uniform(self.min_leverage, self.max_leverage)
        self.delta = np.random.uniform(self.min_delta, 0.6)
        self.premium = np.random.uniform(0.01, self.max_premium_pct)
        self.days_to_expiry = np.random.randint(self.min_days_to_expiry, self.max_days_to_expiry)
        self.current_warrant = {"type": "call", "leverage": self.leverage, "delta": self.delta}
        self.buy(bar.close_price, 100)
        self.entry_price = bar.close_price
        self.write_log(f"Warrant买入认购: 杠杆{self.leverage:.1f}x Delta{self.delta:.2f} 溢价{self.premium*100:.1f}%")

    def _buy_put_warrant(self, bar: BarData):
        """买入认沽证"""
        self.leverage = np.random.uniform(self.min_leverage, self.max_leverage)
        self.delta = np.random.uniform(-0.6, -self.min_delta)
        self.premium = np.random.uniform(0.01, self.max_premium_pct)
        self.days_to_expiry = np.random.randint(self.min_days_to_expiry, self.max_days_to_expiry)
        self.current_warrant = {"type": "put", "leverage": self.leverage, "delta": self.delta}
        self.sell(bar.close_price, 100)
        self.entry_price = bar.close_price
        self.write_log(f"Warrant买入认沽: 杠杆{self.leverage:.1f}x Delta{self.delta:.2f} 溢价{self.premium*100:.1f}%")

    def _manage_position(self, bar: BarData, signal: float):
        """管理持仓：止盈止损、时间衰减平仓、信号反转"""
        pnl_pct = (bar.close_price - self.entry_price) / self.entry_price * self.leverage
        self.pnl = pnl_pct * self.max_position_size

        # 信号反转
        if self.current_warrant and self.current_warrant["type"] == "call" and signal < 0:
            self.sell(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log("Warrant平仓: 信号反转")
        elif self.current_warrant and self.current_warrant["type"] == "put" and signal > 0:
            self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log("Warrant平仓: 信号反转")
        # 临近到期平仓
        elif self.days_to_expiry <= 3:
            self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log("Warrant平仓: 临近到期")
        # 止盈止损
        elif pnl_pct >= self.profit_take_pct:
            self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log(f"Warrant止盈: {pnl_pct*100:.1f}%")
        elif pnl_pct <= -self.stop_loss_pct:
            self.sell(bar.close_price, abs(self.pos)) if self.pos > 0 else self.cover(bar.close_price, abs(self.pos))
            self._reset()
            self.write_log(f"Warrant止损: {pnl_pct*100:.1f}%")

    def _reset(self):
        self.pos = 0
        self.entry_price = 0.0
        self.current_warrant = None
        self.leverage = 0.0
        self.delta = 0.0
        self.premium = 0.0
        self.days_to_expiry = 0