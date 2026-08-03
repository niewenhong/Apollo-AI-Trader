"""
strategies/options/base_option_strategy.py - 期权策略基类 v1.2
"""
import logging
from typing import Optional, Dict, Any

from vnpy.trader.constant import Interval, Direction, Offset, OrderType, Status
from vnpy.trader.object import TickData, BarData, OrderData, TradeData
from vnpy_ctastrategy import CtaTemplate, StopOrder

logger = logging.getLogger(__name__)


class BaseOptionStrategy(CtaTemplate):
    """期权策略基类，提供期权通用的参数、函数和风控"""

    author = "Apollo AI Trader"

    # 期权通用参数
    delta_target = 0.3          # 目标Delta绝对值
    min_delta_abs = 0.2         # 最小Delta绝对值
    max_delta_abs = 0.5         # 最大Delta绝对值
    min_premium = 0.01          # 最低权利金（美元）
    max_premium = 0.50          # 最高权利金（美元）
    expiry_days = 30            # 目标到期天数
    min_expiry_days = 7         # 最短到期天数
    max_expiry_days = 90        # 最长到期天数
    position_pct = 0.08         # 仓位百分比
    fixed_size = 1              # 固定手数
    stop_loss = 0.03            # 止损比例
    take_profit = 0.15          # 止盈比例
    max_positions = 5           # 最大持仓数量
    use_trailing_stop = False   # 是否使用移动止损
    trailing_stop_pct = 0.02    # 移动止损比例

    # 变量
    current_price = 0.0
    position_value = 0.0
    unrealized_pnl = 0.0
    realized_pnl = 0.0
    trade_count = 0
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    atr_value = 0.0

    parameters = [
        "delta_target", "min_delta_abs", "max_delta_abs",
        "min_premium", "max_premium",
        "expiry_days", "min_expiry_days", "max_expiry_days",
        "position_pct", "fixed_size",
        "stop_loss", "take_profit", "max_positions",
        "use_trailing_stop", "trailing_stop_pct"
    ]
    variables = [
        "current_price", "position_value",
        "unrealized_pnl", "realized_pnl", "trade_count",
        "entry_price", "highest_price", "lowest_price", "atr_value"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # ★ 修复：添加 Interval 导入并使用
        self.interval = Interval.DAILY  # 默认使用日线

        # 初始化参数
        self.delta_target = setting.get("delta_target", 0.3)
        self.min_delta_abs = setting.get("min_delta_abs", 0.2)
        self.max_delta_abs = setting.get("max_delta_abs", 0.5)
        self.min_premium = setting.get("min_premium", 0.01)
        self.max_premium = setting.get("max_premium", 0.50)
        self.expiry_days = setting.get("expiry_days", 30)
        self.min_expiry_days = setting.get("min_expiry_days", 7)
        self.max_expiry_days = setting.get("max_expiry_days", 90)
        self.position_pct = setting.get("position_pct", 0.08)
        self.fixed_size = setting.get("fixed_size", 1)
        self.stop_loss = setting.get("stop_loss", 0.03)
        self.take_profit = setting.get("take_profit", 0.15)
        self.max_positions = setting.get("max_positions", 5)
        self.use_trailing_stop = setting.get("use_trailing_stop", False)
        self.trailing_stop_pct = setting.get("trailing_stop_pct", 0.02)

        # 初始化变量
        self.current_price = 0.0
        self.position_value = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.entry_price = 0.0
        self.highest_price = 0.0
        self.lowest_price = 0.0
        self.atr_value = 0.0

        # 期权专用变量
        self.option_chain = []          # 期权链数据
        self.selected_option = None     # 当前选择的期权合约
        self.iv_current = 0.0           # 当前隐含波动率
        self.iv_percentile = 0.5        # IV百分位
        self.delta_current = 0.0        # 当前Delta
        self.gamma_current = 0.0        # 当前Gamma
        self.theta_current = 0.0        # 当前Theta
        self.vega_current = 0.0         # 当前Vega

        logger.info(f"[BaseOption] {strategy_name} 初始化完成")

    def on_init(self):
        """策略初始化回调"""
        self.write_log(f"{self.strategy_name} 初始化完成")
        self.load_bar(10)  # 加载10根K线

    def on_start(self):
        """策略启动回调"""
        self.write_log(f"{self.strategy_name} 启动")
        self.put_event()

    def on_stop(self):
        """策略停止回调"""
        self.write_log(f"{self.strategy_name} 停止")
        self.put_event()

    def on_tick(self, tick: TickData):
        """Tick行情回调（期权策略通常不使用Tick，留空）"""
        pass

    def on_bar(self, bar: BarData):
        """Bar回调（子类实现具体逻辑）"""
        pass

    def on_order(self, order: OrderData):
        """委托回调"""
        pass

    def on_trade(self, trade: TradeData):
        """成交回调"""
        self.trade_count += 1
        if trade.direction == Direction.LONG:
            self.entry_price = trade.price
        self.realized_pnl += trade.profit if hasattr(trade, 'profit') else 0
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        """停止单回调"""
        pass

    # ==================== 风控与辅助方法 ====================

    def calculate_position_size(self, price: float, account_value: float) -> int:
        """根据账户价值和风险参数计算开仓数量"""
        risk_per_trade = account_value * self.position_pct
        if price <= 0:
            return 0
        size = int(risk_per_trade / (price * self.stop_loss))
        return max(1, min(size, self.fixed_size))

    def check_stop_loss(self, current_price: float) -> bool:
        """检查是否触发止损"""
        if self.pos == 0 or self.entry_price == 0:
            return False
        if self.pos > 0:
            loss_pct = (self.entry_price - current_price) / self.entry_price
            return loss_pct >= self.stop_loss
        else:
            loss_pct = (current_price - self.entry_price) / self.entry_price
            return loss_pct >= self.stop_loss

    def check_take_profit(self, current_price: float) -> bool:
        """检查是否触发止盈"""
        if self.pos == 0 or self.entry_price == 0:
            return False
        if self.pos > 0:
            profit_pct = (current_price - self.entry_price) / self.entry_price
            return profit_pct >= self.take_profit
        else:
            profit_pct = (self.entry_price - current_price) / self.entry_price
            return profit_pct >= self.take_profit

    def update_trailing_stop(self, current_price: float):
        """更新移动止损价（子类可重写）"""
        if not self.use_trailing_stop:
            return
        if self.pos > 0:
            if current_price > self.highest_price:
                self.highest_price = current_price
        elif self.pos < 0:
            if current_price < self.lowest_price:
                self.lowest_price = current_price

    def get_trailing_stop_price(self) -> float:
        """获取移动止损价格"""
        if self.pos > 0:
            return self.highest_price * (1 - self.trailing_stop_pct)
        elif self.pos < 0:
            return self.lowest_price * (1 + self.trailing_stop_pct)
        return 0.0

    def write_log(self, msg: str):
        """写日志并推送事件"""
        logger.info(f"[{self.strategy_name}] {msg}")
        self.put_event()