"""
strategies/sell_put_strategy.py - v2.6.0
卖出Put期权策略：在看好标的时卖出虚值Put，收取权利金
"""
import time
import math
from datetime import datetime, timedelta
from vnpy.trader.object import OrderData, TradeData, TickData
from vnpy.trader.constant import OrderType, Direction, Offset, Status


class SellPutStrategy:
    """
    卖出Put策略
    - 监控标的股价
    - 当价格高于行权价阈值时，卖出虚值Put收取权利金
    - 自动管理持仓和止损
    """

    author = "Apollo AI Trader"
    parameters = [
        "strike_percent",
        "expiry_days",
        "premium_target",
        "fixed_size",
        "max_position",
        "min_cash_reserve"
    ]
    variables = [
        "pos",
        "last_trade_time",
        "total_premium",
        "trades_count"
    ]

    def __init__(self, strategy_engine, strategy_name, vt_symbol, setting):
        self.strategy_engine = strategy_engine
        self.strategy_name = strategy_name
        self.vt_symbol = vt_symbol
        self.setting = setting

        self.strike_percent = setting.get("strike_percent", 0.92)
        self.expiry_days = setting.get("expiry_days", 30)
        self.premium_target = setting.get("premium_target", 0.025)
        self.fixed_size = setting.get("fixed_size", 1)
        self.max_position = setting.get("max_position", 5)
        self.min_cash_reserve = setting.get("min_cash_reserve", 10000)

        self.pos = 0
        self.last_trade_time = ""
        self.total_premium = 0.0
        self.trades_count = 0

        self.main_engine = None
        self._tick = None
        self._underlying_price = 0.0
        self._option_symbol = ""
        self._last_check = 0

    def on_init(self):
        self.write_log(f"策略 {self.strategy_name} 初始化完成")
        self.write_log(f"  标的: {self.vt_symbol}")
        self.write_log(f"  行权价比例: {self.strike_percent}")
        self.write_log(f"  到期天数: {self.expiry_days}")
        self.write_log(f"  目标权利金率: {self.premium_target}")

    def on_start(self):
        self.write_log(f"策略 {self.strategy_name} 已启动")

    def on_stop(self):
        self.write_log(f"策略 {self.strategy_name} 已停止")

    def on_tick(self, tick: TickData):
        self._tick = tick
        self._underlying_price = tick.last_price

        now = time.time()
        if now - self._last_check < 30:
            return
        self._last_check = now

        if self.pos >= self.max_position:
            return
        if not self._is_trading_time():
            return

        self._try_sell_put()

    def on_bar(self, bar):
        pass

    def on_trade(self, trade: TradeData):
        self.pos += trade.volume if trade.direction == Direction.SHORT else -trade.volume
        self.total_premium += trade.price * trade.volume
        self.trades_count += 1
        self.last_trade_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.write_log(
            f"成交: {trade.direction.value} {trade.volume}张 "
            f"@ {trade.price:.2f} 总权利金: {self.total_premium:.2f}"
        )

    def on_order(self, order: OrderData):
        if order.status == Status.REJECTED:
            self.write_log(f"⚠️ 订单被拒: {order.rejected_reason}")
        elif order.status == Status.CANCELLED:
            self.write_log(f"订单已撤销: {order.vt_orderid}")

    def _is_trading_time(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        if now.hour < 9 or now.hour > 22:
            return False
        return True

    def _try_sell_put(self):
        if self._underlying_price <= 0:
            return

        target_strike = round(self._underlying_price * self.strike_percent, 0)
        option_symbol = self._find_put_option(target_strike)

        if not option_symbol:
            self.write_log(f"未找到合适的Put合约 (行权价≈{target_strike})")
            return

        self.write_log(
            f"卖出Put: {option_symbol} | "
            f"行权价 {target_strike} | "
            f"数量 {self.fixed_size} | "
            f"标的价 {self._underlying_price:.2f}"
        )

        self.strategy_engine.send_order(
            strategy=self,
            vt_symbol=option_symbol,
            direction=Direction.SHORT,
            offset=Offset.OPEN,
            volume=self.fixed_size,
            order_type=OrderType.MARKET,
            price=None
        )

    def _find_put_option(self, target_strike: float) -> str:
        try:
            contracts = self.main_engine.get_all_contracts()
            best_symbol = ""
            best_diff = float("inf")
            for contract in contracts:
                if getattr(contract, 'option_type', '') != "PUT":
                    continue
                if getattr(contract, 'underlying', '') != self.vt_symbol:
                    continue
                strike_diff = abs(getattr(contract, 'strike_price', 0) - target_strike)
                if strike_diff < best_diff:
                    best_diff = strike_diff
                    best_symbol = contract.vt_symbol
            return best_symbol
        except Exception as e:
            self.write_log(f"查找Put合约失败: {e}")
            return ""

    def write_log(self, msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{self.strategy_name}] {msg}"
        print(f"{timestamp} | {log_msg}")
        try:
            if hasattr(self.strategy_engine, 'db') and self.strategy_engine.db:
                self.strategy_engine.db.log_event(log_msg)
        except Exception:
            pass
