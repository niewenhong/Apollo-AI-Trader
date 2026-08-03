"""
strategies/structured_products/warrant_strategy.py - v3.2.0
港股涡轮策略（修复：移除 FinancialQuota 导入）
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData
# from futu import FinancialQuota  # 当前版本未使用，注释掉


class WarrantStrategy(CtaTemplate):
    author = "Apollo"

    parameters = [
        "warrant_type",       # CALL / PUT
        "entry_delta",        # 入场 delta 阈值
        "exit_delta",         # 出场 delta 阈值
        "fixed_size",
    ]

    variables = [
        "pos", "delta", "underlying_price", "pnl"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.warrant_type = setting.get("warrant_type", "CALL")
        self.entry_delta = setting.get("entry_delta", 0.3)
        self.exit_delta = setting.get("exit_delta", 0.1)
        self.fixed_size = setting.get("fixed_size", 10000)

        self.pos = 0
        self.delta = 0.0
        self.underlying_price = 0.0
        self.pnl = 0.0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.load_bar(10, use_database=True)
        self.write_log("Warrant策略初始化完成（占位）")

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

        # 占位逻辑：实际应通过富途查询涡轮实时数据
        self.delta = 0.5  # 模拟
        self.underlying_price = bar.close_price

        if self.pos == 0:
            if self.delta > self.entry_delta:
                self.buy(bar.close_price, self.fixed_size)
                self.write_log("涡轮多头入场")
        else:
            if self.delta < self.exit_delta:
                self.sell(bar.close_price, abs(self.pos))
                self.write_log("涡轮多头出场")