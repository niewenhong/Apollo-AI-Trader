"""
strategies/equity/order_flow_strategy.py — Apollo-AI-Trader v2.8.0
TickOrderFlowStrategy 完整版

v2.8.0 修正：
- 补全所有 vnpy 标准导入
- 修复 pytz 依赖（使用标准库 zoneinfo 替代）
- 类型注解完整
"""
import time
from datetime import datetime, time as dtime
from typing import Optional, Tuple, Callable

# ====== vnpy 标准导入 ======
from vnpy.trader.constant import Direction, Offset, Status, Interval, Exchange
from vnpy.trader.object import BarData, TickData, OrderData, TradeData, PositionData
from vnpy.trader.utility import ArrayManager, extract_vt_symbol
from vnpy_ctastrategy import CtaTemplate, StopOrder

# 时区处理：优先用 zoneinfo（Python 3.9+），回退到 pytz
try:
    from zoneinfo import ZoneInfo
    _US_TZ = ZoneInfo("America/New_York")
    _HK_TZ = ZoneInfo("Asia/Hong_Kong")
except ImportError:
    import pytz  # type: ignore
    _US_TZ = pytz.timezone("America/New_York")
    _HK_TZ = pytz.timezone("Asia/Hong_Kong")


class TickOrderFlowStrategy(CtaTemplate):
    """
    Tick 订单流策略：
    - 基于买卖盘口 imbalance 判断方向
    - Kelly 动态仓位
    - 移动止盈 + 固定止损
    - 尾盘强制清仓
    """
    author = "Apollo-AI-Trader"

    # ── 参数 ──
    bar_window = 1
    volume_multiplier = 2.5
    imbalance_threshold = 0.65
    stop_loss_pct = 0.008
    ai_score = 0.70
    backtest_win_loss_ratio = 1.8
    sentiment_index = 0.50

    profit_activation_pct = 0.020
    profit_rollback_pct = 0.005

    # ── 变量（供 CTA 引擎显示）──
    current_trend = 1
    last_price = 0.0
    long_pos = 0
    entry_price = 0.0
    stop_line = 0.0

    trailing_profit_active = False
    highest_price_since_entry = 0.0
    profit_target_line = 0.0
    is_ordering = False

    today_pnl = 0.0
    total_trades = 0
    winning_trades = 0

    parameters = [
        "bar_window", "volume_multiplier", "imbalance_threshold", "stop_loss_pct",
        "ai_score", "backtest_win_loss_ratio", "sentiment_index",
        "profit_activation_pct", "profit_rollback_pct"
    ]
    variables = [
        "current_trend", "last_price", "long_pos", "entry_price", "stop_line",
        "trailing_profit_active", "highest_price_since_entry", "profit_target_line",
        "today_pnl", "total_trades", "is_ordering"
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 从 setting 加载参数
        self.load_strategy_setting(setting)

        # 判断市场
        self.is_us_market = ".SMART" in vt_symbol or ".US." in vt_symbol
        self.market_tz = _US_TZ if self.is_us_market else _HK_TZ

        # 回调（外部注入通知函数）
        self.notice_callback: Optional[Callable] = None

        self.write_log(f"[INIT] {strategy_name} | {vt_symbol} | US={self.is_us_market}")

    def load_strategy_setting(self, setting: dict):
        self.bar_window = setting.get("bar_window", self.bar_window)
        self.volume_multiplier = setting.get("volume_multiplier", self.volume_multiplier)
        self.imbalance_threshold = setting.get("imbalance_threshold", self.imbalance_threshold)
        self.stop_loss_pct = setting.get("stop_loss_pct", self.stop_loss_pct)
        self.ai_score = setting.get("ai_score", self.ai_score)
        self.backtest_win_loss_ratio = setting.get("backtest_win_loss_ratio", self.backtest_win_loss_ratio)
        self.sentiment_index = setting.get("sentiment_index", self.sentiment_index)
        self.profit_activation_pct = setting.get("profit_activation_pct", self.profit_activation_pct)
        self.profit_rollback_pct = setting.get("profit_rollback_pct", self.profit_rollback_pct)

    # ──────────────────────────────
    #  生命周期
    # ──────────────────────────────
    def on_init(self):
        self.inited = True
        self.write_log(f"[on_init] ✅ 初始化完成")

    def on_start(self):
        self.is_ordering = False
        self.trailing_profit_active = False
        self.highest_price_since_entry = 0.0
        self.write_log(f"[on_start] ▶️ 策略已激活")

    def on_stop(self):
        self.write_log(f"[on_stop] ⏸ 策略已停止 | pos={self.long_pos} pnl={self.today_pnl:.2f}")

    # ──────────────────────────────
    #  时间窗口
    # ──────────────────────────────
    def check_time_window(self) -> Tuple[bool, bool]:
        """返回 (allow_open, must_close)"""
        now_in_market = datetime.now(self.market_tz).time()
        if self.is_us_market:
            allow_open = dtime(9, 45, 0) <= now_in_market < dtime(15, 50, 0)
            must_close = now_in_market >= dtime(15, 50, 0)
        else:
            allow_am = dtime(9, 45, 0) <= now_in_market < dtime(11, 55, 0)
            allow_pm = dtime(13, 15, 0) <= now_in_market < dtime(15, 50, 0)
            allow_open = allow_am or allow_pm
            must_close = (dtime(11, 55, 0) <= now_in_market < dtime(12, 0, 0)) or (now_in_market >= dtime(15, 50, 0))
        return allow_open, must_close

    # ──────────────────────────────
    #  Kelly 仓位
    # ──────────────────────────────
    def calculate_kelly_volume(self, current_price: float) -> int:
        p = self.ai_score
        q = 1.0 - p
        b = self.backtest_win_loss_ratio
        if b <= 0 or current_price <= 0:
            return 100
        f_star = (p - (q / b)) * 0.5
        f_optimized = min(max(f_star * (0.5 + self.sentiment_index), 0.01), 0.08)

        if self.is_us_market:
            available = 50000.0
            lot = 1
        else:
            available = 810000.0
            lot = 100

        raw = (available * f_optimized / current_price) / lot
        return int(max(round(raw) * lot, lot))

    # ──────────────────────────────
    #  K线 / Tick
    # ──────────────────────────────
    def on_bar(self, bar: BarData):
        self.last_price = bar.close_price

    def on_tick(self, tick: TickData):
        self.last_price = tick.last_price

        if self.is_ordering:
            return
        if self.long_pos <= 0 and self.sentiment_index < 0.45:
            return

        allow_open, must_close = self.check_time_window()

        # 强制清仓
        if must_close:
            if self.long_pos > 0:
                self.is_ordering = True
                if self.notice_callback:
                    self.notice_callback(self.vt_symbol, tick.last_price, "🛑 刚性清仓", "触发尾盘风控强制收兵")
                self.sell(tick.last_price - 0.05, self.long_pos)
            return

        # 持仓管理
        if self.long_pos > 0:
            if tick.last_price > self.highest_price_since_entry:
                self.highest_price_since_entry = tick.last_price

            # 激活移动止盈
            if not self.trailing_profit_active:
                if tick.last_price >= self.entry_price * (1.0 + self.profit_activation_pct):
                    self.trailing_profit_active = True
                    self.profit_target_line = round(self.highest_price_since_entry * (1.0 - self.profit_rollback_pct), 3)
                    if self.notice_callback:
                        self.notice_callback(self.vt_symbol, tick.last_price, "🎯 移动止盈激活",
                                             f"保护线: {self.profit_target_line}")
            else:
                # 更新保护线
                new_line = round(tick.last_price * (1.0 - self.profit_rollback_pct), 3)
                if new_line > self.profit_target_line:
                    self.profit_target_line = new_line

            # 移动止盈触发
            if self.trailing_profit_active and tick.last_price <= self.profit_target_line:
                self.is_ordering = True
                if self.notice_callback:
                    self.notice_callback(self.vt_symbol, tick.last_price, "💰 追踪止盈",
                                         f"跌破保护线 {self.profit_target_line}")
                self.sell(tick.last_price - 0.05, self.long_pos)
                return

            # 止损触发
            if not self.trailing_profit_active and tick.last_price <= self.stop_line:
                self.is_ordering = True
                if self.notice_callback:
                    self.notice_callback(self.vt_symbol, tick.last_price, "🚨 止损",
                                         f"跌破 {self.stop_line}")
                self.sell(tick.last_price - 0.05, self.long_pos)
                return

            # 更新止损线
            if not self.trailing_profit_active:
                new_stop = tick.last_price * (1.0 - self.stop_loss_pct)
                if new_stop > self.stop_line:
                    self.stop_line = round(new_stop, 3)
            return

        # 不开仓时段
        if not allow_open:
            return

        # ── 计算盘口 imbalance ──
        try:
            w1, w2, w3, w4, w5 = 0.40, 0.25, 0.15, 0.12, 0.08
            total_bid = (tick.bid_volume_1 * w1 + tick.bid_volume_2 * w2 +
                         tick.bid_volume_3 * w3 + tick.bid_volume_4 * w4 + tick.bid_volume_5 * w5)
            total_ask = (tick.ask_volume_1 * w1 + tick.ask_volume_2 * w2 +
                         tick.ask_volume_3 * w3 + tick.ask_volume_4 * w4 + tick.ask_volume_5 * w5)
            total_book = total_bid + total_ask
            if total_book == 0:
                return
            imbalance = total_bid / total_book
        except AttributeError:
            # 降级：只用一档
            total_book = tick.bid_volume_1 + tick.ask_volume_1
            if total_book == 0:
                return
            imbalance = tick.bid_volume_1 / total_book

        # 开仓信号
        if imbalance > self.imbalance_threshold:
            self.is_ordering = True
            order_volume = self.calculate_kelly_volume(tick.last_price)
            if self.notice_callback:
                self.notice_callback(self.vt_symbol, tick.last_price, "🟢 AI 开仓",
                                     f"imbalance={imbalance:.2f} vol={order_volume}")
            self.buy(tick.last_price + 0.05, order_volume)

    # ──────────────────────────────
    #  回调
    # ──────────────────────────────
    def on_order(self, order: OrderData):
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self.is_ordering = False

    def on_trade(self, trade: TradeData):
        if trade.direction == Direction.LONG:
            self.long_pos += trade.volume
            self.entry_price = trade.price
            self.stop_line = round(trade.price * (1.0 - self.stop_loss_pct), 3)
            self.highest_price_since_entry = trade.price
            self.trailing_profit_active = False
            self.write_log(f"[on_trade] BUY {trade.volume}@{trade.price:.2f} | pos={self.long_pos}")
        else:
            pnl = (trade.price - self.entry_price) * trade.volume if self.entry_price > 0 else 0.0
            self.today_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1
            self.long_pos = max(0, self.long_pos - trade.volume)
            if self.long_pos == 0:
                self.entry_price = 0.0
                self.stop_line = 0.0
            self.write_log(f"[on_trade] SELL {trade.volume}@{trade.price:.2f} | pnl={pnl:+.2f}")
        self.is_ordering = False
        self.put_event()
