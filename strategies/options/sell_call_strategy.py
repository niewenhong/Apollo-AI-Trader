"""
strategies/options/sell_call_strategy.py - 卖出看涨期权策略 v1.3 (修复版)
"""
import logging
from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import TickData, BarData, OrderData, TradeData
from vnpy_ctastrategy import CtaTemplate, StopOrder

from .base_option_strategy import BaseOptionStrategy

logger = logging.getLogger(__name__)


class SellCallStrategy(BaseOptionStrategy):
    """卖出看涨期权策略（备兑开仓或裸卖）"""

    author = "Apollo AI Trader"

    # 额外参数
    short_call_strike_offset = 0.05
    roll_when_itm_pct = 0.80
    max_dte_roll = 7
    min_premium_collect = 0.02

    # ★ 变量列表：必须与 __init__ 中初始化的实例属性完全一致
    variables = [
        "net_premium",
        "current_option_price",
        "option_delta",
        "option_theta",
        "option_iv",
        "days_to_expiry",
        "strike_price",
        "is_itm",
        "is_rolled",
        "last_roll_date",
        # 继承自父类的变量
        "current_price", "position_value",
        "unrealized_pnl", "realized_pnl", "trade_count",
        "entry_price", "highest_price", "lowest_price", "atr_value"
    ]

    parameters = [
        "short_call_strike_offset", "roll_when_itm_pct", "max_dte_roll",
        "min_premium_collect",
        "delta_target", "min_delta_abs", "max_delta_abs",
        "min_premium", "max_premium",
        "expiry_days", "min_expiry_days", "max_expiry_days",
        "position_pct", "fixed_size",
        "stop_loss", "take_profit", "max_positions",
        "use_trailing_stop", "trailing_stop_pct"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        # ★ 先调用父类初始化（父类会设置一些属性）
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # ★ 强制初始化所有在 variables 中声明的属性（防止任何遗漏）
        self.net_premium = 0.0
        self.current_option_price = 0.0
        self.option_delta = 0.0
        self.option_theta = 0.0
        self.option_iv = 0.0
        self.days_to_expiry = 0
        self.strike_price = 0.0
        self.is_itm = False
        self.is_rolled = False
        self.last_roll_date = ""

        # 额外参数
        self.short_call_strike_offset = setting.get("short_call_strike_offset", 0.05)
        self.roll_when_itm_pct = setting.get("roll_when_itm_pct", 0.80)
        self.max_dte_roll = setting.get("max_dte_roll", 7)
        self.min_premium_collect = setting.get("min_premium_collect", 0.02)

        logger.info(f"[SellCall] {strategy_name} 初始化完成")

    def on_init(self):
        """策略初始化"""
        self.write_log("SellCallStrategy 初始化")
        self.load_bar(10)

    def on_start(self):
        """策略启动"""
        self.write_log("SellCallStrategy 启动")
        self.put_event()

    def on_stop(self):
        """策略停止"""
        self.write_log("SellCallStrategy 停止")
        self.put_event()

    def on_tick(self, tick: TickData):
        """Tick行情回调"""
        self.current_price = tick.last_price
        self.put_event()

    def on_bar(self, bar: BarData):
        """Bar回调 - 简单示例逻辑"""
        self.current_price = bar.close_price
        # 更新一些变量以避免被优化掉
        self.days_to_expiry = self.expiry_days
        self.strike_price = self.current_price * 1.05
        self.option_delta = -0.3
        self.option_theta = 0.002
        self.option_iv = 0.25
        self.is_itm = self.current_price > self.strike_price
        self.current_option_price = max(self.min_premium, self.current_price * 0.02)
        self.put_event()

    def on_order(self, order: OrderData):
        """委托回调"""
        pass

    def on_trade(self, trade: TradeData):
        """成交回调"""
        self.trade_count += 1
        if trade.direction == Direction.SHORT and trade.offset == Offset.OPEN:
            self.entry_price = trade.price
        self.realized_pnl += trade.profit if hasattr(trade, 'profit') else 0
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        """停止单回调"""
        pass