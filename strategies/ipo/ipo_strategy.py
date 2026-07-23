"""
strategies/ipo/ipo_strategy.py - Apollo-AI-Trader v2.6.0
IPO策略：新股申购 + 首日交易
- 申购筛选：估值/行业/绿鞋/基石
- 首日：开盘观察 → 突破入场 → 止盈止损
"""
from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, OrderType
from datetime import datetime, timedelta
import time


class IPOStrategy(CtaTemplate):
    author = "Apollo"
    version = "v2.6.0"

    # 申购筛选参数
    min_subscribe_ratio = 50.0    # 最低超额认购倍数
    max_pe_ratio = 30.0          # 最高PE倍数
    require_greenshoe = True     # 要求绿鞋
    min_foundation_ratio = 0.20  # 最低基石占比

    # 首日交易参数
    first_day_max_hold_min = 240  # 首日最大持有分钟(4小时)
    profit_take_pct = 0.30       # 止盈30%
    stop_loss_pct = 0.15         # 止损15%
    open_observe_min = 5         # 开盘观察分钟数
    breakout_threshold = 0.05    # 突破阈值5%
    max_capital_per_ipo = 50000  # 单只新股最大资金

    # 策略参数
    fixed_size = 500
    is_simulate = True

    parameters = [
        "min_subscribe_ratio", "max_pe_ratio", "require_greenshoe",
        "min_foundation_ratio", "first_day_max_hold_min", "profit_take_pct",
        "stop_loss_pct", "open_observe_min", "breakout_threshold",
        "max_capital_per_ipo", "fixed_size", "is_simulate",
    ]
    variables = ["pos", "entry_price", "entry_time", "highest", "lowest",
                 "status", "ipos_tracked", "today_ipo"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.pos = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.highest = 0.0
        self.lowest = 999999.0
        self.status = "idle"  # idle/subscribed/observing/holding/closed
        self.ipos_tracked = 0
        self.today_ipo = False
        self._open_price = 0.0
        self._bars_since_open = 0
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(size=100)

    def on_init(self):
        self.write_log(f"[IPO] on_init | {self.vt_symbol}")

    def on_start(self):
        self.write_log(f"[IPO] on_start | 监控新股上市")

    def on_stop(self):
        self.write_log(f"[IPO] on_stop")

    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited: return

        # 检测是否为新股首日
        if not self.today_ipo:
            self.today_ipo = self._is_ipo_first_day(bar)
            if self.today_ipo:
                self.write_log(f"[IPO] 🆕 检测到新股首日: {self.vt_symbol}")
                self.status = "observing"
                self._open_price = bar.open_price
                self._bars_since_open = 0

        if self.status == "observing":
            self._bars_since_open += 1
            # 观察期结束，判断是否入场
            if self._bars_since_open >= self.open_observe_min:
                self._evaluate_entry(bar)
        elif self.status == "holding":
            self._manage_holding(bar)

    def _is_ipo_first_day(self, bar) -> bool:
        """判断是否为IPO首日（简化：通过价格突变检测）"""
        # 实际应接入新股日历API
        # 简化：如果AM里前N根都是0或缺失，突然有价格 → 可能是首日
        closes = self.am.close[-20:] if len(self.am.close)>=20 else self.am.close
        zero_count = sum(1 for c in closes if c == 0)
        if zero_count > 15 and bar.close_price > 0:
            return True
        return False

    def _evaluate_entry(self, bar: BarData):
        """观察期结束后评估是否入场"""
        change = (bar.close_price - self._open_price) / self._open_price
        # 突破上行
        if change >= self.breakout_threshold:
            self._buy(bar.close_price)
            self.write_log(f"[IPO] 突破入场: +{change*100:.1f}% @ {bar.close_price:.2f}")
        # 跌破开盘价太多 → 放弃
        elif change < -self.stop_loss_pct:
            self.write_log(f"[IPO] 开盘即破发{change*100:.1f}%，放弃")
            self.status = "closed"
            self.today_ipo = False
        else:
            # 继续观察
            self.status = "observing"
            self.write_log(f"[IPO] 继续观察: {change*100:.1f}%")

    def _buy(self, price):
        try:
            size = min(self.fixed_size, int(self.max_capital_per_ipo / price))
            vt = self.buy(price + 0.01, size)
            self.write_log(f"[IPO] ✅ 买入 {size}股 @ {price:.2f}")
        except Exception as e:
            self.write_log(f"[IPO] 买入失败: {e}")

    def _manage_holding(self, bar: BarData):
        """管理首日持仓"""
        if self.pos > 0:
            self.highest = max(self.highest, bar.close_price)
            self.lowest = min(self.lowest, bar.close_price)
            # 止盈
            if bar.close_price >= self.entry_price * (1 + self.profit_take_pct):
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(f"[IPO] 🎯 止盈 +{self.profit_take_pct*100:.0f}%")
            # 止损
            elif bar.close_price <= self.entry_price * (1 - self.stop_loss_pct):
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(f"[IPO] 🛑 止损 -{self.stop_loss_pct*100:.0f}%")
            # 超时平仓（收盘前）
            elif self.entry_time:
                elapsed = (datetime.now() - self.entry_time).seconds / 60
                if elapsed >= self.first_day_max_hold_min:
                    self.sell(bar.close_price - 0.01, abs(self.pos))
                    self.write_log(f"[IPO] ⏰ 超时平仓 {elapsed:.0f}分钟")

    def on_order(self, order: OrderData):
        if order.traded > 0 and order.direction == Direction.LONG:
            self.pos = order.traded
            self.entry_price = order.price
            self.entry_time = datetime.now()
            self.status = "holding"
            self.highest = order.price
        self.put_event()

    def on_trade(self, trade: TradeData):
        if trade.direction == Direction.LONG:
            self.pos += trade.volume
            self.entry_price = trade.price
            self.entry_time = datetime.now()
            self.status = "holding"
        elif trade.direction == Direction.SHORT:
            self.pos -= trade.volume
            self.status = "closed"
        self.put_event()
