"""
strategies/futures/momentum_strategy.py - v3.0.0
期货动量策略（占位/研究用）
当前仅做框架，待期货数据源就绪后可启用

变更 v3.0.0：
  - ★ 显式初始化所有 parameters（防止 setting 缺失导致 AttributeError）
  - 兼容 vnpy CtaTemplate 的 setting 字典注入
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData


class MomentumStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "momentum_window",
        "entry_threshold",
        "fixed_size",
        "stop_loss_atr",
    ]

    variables = [
        "pos", "momentum", "atr_value", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # ★ 显式初始化所有参数（防止 setting 缺失导致 AttributeError）
        self.momentum_window = setting.get("momentum_window", 20)
        self.entry_threshold = setting.get("entry_threshold", 0.02)
        self.fixed_size = setting.get("fixed_size", 1)
        self.stop_loss_atr = setting.get("stop_loss_atr", 2.0)

        self.pos = 0
        self.momentum = 0.0
        self.atr_value = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("Momentum策略初始化完成（占位）")

    def on_start(self):
        self.write_log("Momentum策略启动")

    def on_stop(self):
        self.write_log("Momentum策略停止")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        if len(self.am.close) > self.momentum_window:
            self.momentum = (self.am.close[-1] - self.am.close[-self.momentum_window]) / (
                self.am.close[-self.momentum_window] + 1e-6
            )
        self.atr_value = self.am.atr(14, array=False)

        if self.pos == 0:
            if self.momentum > self.entry_threshold:
                self.buy(bar.close_price, self.fixed_size)
                self.write_log(f"动量多头入场 | momentum={self.momentum:.4f}")
            elif self.momentum < -self.entry_threshold:
                self.short(bar.close_price, self.fixed_size)
                self.write_log(f"动量空头入场 | momentum={self.momentum:.4f}")
        else:
            if self.pos > 0 and bar.close_price < self.am.close[-1] - self.atr_value * self.stop_loss_atr:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log(f"多头止损出场 | atr={self.atr_value:.2f}")
            elif self.pos < 0 and bar.close_price > self.am.close[-1] + self.atr_value * self.stop_loss_atr:
                self.cover(bar.close_price, abs(self.pos))
                self.write_log(f"空头止损出场 | atr={self.atr_value:.2f}")
