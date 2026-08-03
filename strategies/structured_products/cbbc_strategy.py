"""
strategies/structured_products/cbbc_strategy.py - v3.2.0
港股牛熊证策略（修复：移除 FinancialQuota 导入）
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
# from futu import FinancialQuota  # 当前版本未使用，注释掉


class CBBCStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "cbbc_type",          # BULL / BEAR
        "call_price",         # 收回价
        "entry_distance",     # 入场距离收回价的百分比
        "fixed_size",
    ]

    variables = [
        "pos", "distance_to_call", "underlying_price", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.cbbc_type = setting.get("cbbc_type", "BULL")
        self.call_price = setting.get("call_price", 0.0)
        self.entry_distance = setting.get("entry_distance", 0.05)
        self.fixed_size = setting.get("fixed_size", 10000)

        self.pos = 0
        self.distance_to_call = 0.0
        self.underlying_price = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("CBBC策略初始化完成（占位）")

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

        self.underlying_price = bar.close_price
        if self.call_price > 0:
            self.distance_to_call = abs(self.underlying_price - self.call_price) / self.call_price
        else:
            self.distance_to_call = 999

        if self.pos == 0:
            if self.distance_to_call > self.entry_distance:
                self.buy(bar.close_price, self.fixed_size)
                self.write_log("牛熊证多头入场")
        else:
            if self.distance_to_call < 0.02:  # 接近收回价
                self.sell(bar.close_price, abs(self.pos))
                self.write_log("牛熊证多头出场（接近收回）")