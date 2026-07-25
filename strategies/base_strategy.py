"""
strategies/base_strategy.py - v2.6.0
策略基类：所有策略继承自此基类
提供通用功能：数据库参数加载、日志统一格式、PnL计算、风险检查
"""
from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData, TickData, TradeData
from vnpy.trader.constant import Direction, Offset
from abc import abstractmethod
import json
from datetime import datetime


class ApolloBaseStrategy(CtaTemplate):
    """Apollo策略基类，所有策略继承此类"""

    author = "Apollo"

    # 基类参数（所有策略共用）
    parameters = [
        "enable_db_params",   # 是否启用数据库参数加载
        "risk_check_level",   # 风险检查级别: strict/normal/loose
        "max_daily_loss",     # 每日最大亏损限额
        "max_position_ratio", # 最大仓位比例（相对于账户权益）
    ]

    variables = [
        "pos", "pnl", "daily_pnl", "total_trades",
        "today_loss", "account_value"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.pnl = 0.0
        self.daily_pnl = 0.0
        self.total_trades = 0
        self.today_loss = 0.0
        self.account_value = 1000000.0  # 初始模拟权益

        # 加载数据库参数（如果启用）
        if self.enable_db_params:
            self._load_params_from_db()

    def _load_params_from_db(self):
        """从数据库加载AI优化的参数"""
        try:
            from core.db_manager import CustomDBManager
            db = CustomDBManager()
            ai_params = db.get_latest_params(self.vt_symbol, self.__class__.__name__)
            if ai_params:
                for key, value in ai_params.items():
                    if key in self.parameters:
                        setattr(self, key, value)
                self.write_log(f"[DB] 加载AI参数: {json.dumps(ai_params)}")
        except Exception as e:
            self.write_log(f"[DB] 加载参数失败: {e}")

    def write_log(self, msg: str):
        """统一日志格式"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        super().write_log(f"[{timestamp}] [{self.__class__.__name__}] {msg}")

    def risk_check(self, direction: Direction, price: float, volume: int) -> bool:
        """风险检查：返回True表示允许交易"""
        # 每日亏损限额检查
        if self.today_loss <= -self.max_daily_loss:
            self.write_log(f"[风险] 今日亏损{self.today_loss:.0f}已达限额{self.max_daily_loss:.0f}，暂停交易")
            return False

        # 仓位比例检查
        cost = price * volume * 100  # 假设乘数100
        if cost > self.account_value * self.max_position_ratio:
            self.write_log(f"[风险] 仓位{cost:.0f}超过权益{self.account_value:.0f}的{self.max_position_ratio*100:.0f}%")
            return False

        # 严格模式：检查反向持仓
        if self.risk_check_level == "strict" and self.pos != 0:
            if (self.pos > 0 and direction == Direction.SHORT) or \
               (self.pos < 0 and direction == Direction.LONG):
                self.write_log("[风险] 严格模式不允许反向下单")
                return False

        return True

    def buy(self, price: float, volume: int, **kwargs):
        """重写buy，加入风险检查"""
        if self.risk_check(Direction.LONG, price, volume):
            super().buy(price, volume, **kwargs)
        else:
            self.write_log(f"[阻止] 买入{volume}手 @ {price} 被风险检查拦截")

    def sell(self, price: float, volume: int, **kwargs):
        if self.risk_check(Direction.SHORT, price, volume):
            super().sell(price, volume, **kwargs)

    def short(self, price: float, volume: int, **kwargs):
        if self.risk_check(Direction.SHORT, price, volume):
            super().short(price, volume, **kwargs)

    def cover(self, price: float, volume: int, **kwargs):
        if self.risk_check(Direction.LONG, price, volume):
            super().cover(price, volume, **kwargs)

    def on_trade(self, trade: TradeData):
        """成交回调：更新统计"""
        self.total_trades += 1
        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN:
                self.pos += trade.volume
            else:
                self.pos -= trade.volume
        elif trade.direction == Direction.SHORT:
            if trade.offset == Offset.OPEN:
                self.pos -= trade.volume
            else:
                self.pos += trade.volume

        # 更新PnL
        self.write_log(f"成交: {trade.direction.name} {trade.volume}手 @ {trade.price}")

    def on_bar(self, bar: BarData):
        """子类必须实现"""
        pass

    def on_tick(self, tick: TickData):
        """子类必须实现"""
        pass

    @abstractmethod
    def on_init(self):
        pass

    @abstractmethod
    def on_start(self):
        pass

    @abstractmethod
    def on_stop(self):
        pass