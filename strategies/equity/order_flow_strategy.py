"""
strategies/equity/order_flow_strategy.py - v2.9.6 修正版
Tick 订单流策略（依赖 TICKER 订阅）

修复：
- 继承 BaseStrategy（原 ApolloBaseStrategy 已重命名）
- on_tick 不再调用 super().on_tick()（避免 Tick 重复喂入 bg_1m）
- 盘口 imbalance 防御性降级
- 尾盘强制清仓
- 部分平仓状态一致性修复
"""
import time
from datetime import datetime, time as dtime
from typing import Optional, Tuple

from vnpy.trader.constant import Direction, Status
from vnpy.trader.object import BarData, TickData, OrderData, TradeData

try:
    from zoneinfo import ZoneInfo
    _US_TZ = ZoneInfo("America/New_York")
    _HK_TZ = ZoneInfo("Asia/Hong_Kong")
except ImportError:
    import pytz
    _US_TZ = pytz.timezone("America/New_York")
    _HK_TZ = pytz.timezone("Asia/Hong_Kong")

from strategies.base_strategy import BaseStrategy   # ★ 修复：ApolloBaseStrategy → BaseStrategy


class TickOrderFlowStrategy(BaseStrategy):   # ★ 修复：ApolloBaseStrategy → BaseStrategy
    """
    Tick 级订单流策略：
    - 盘口 imbalance → 方向
    - Kelly 动态仓位
    - 移动止盈 + 硬止损
    - 尾盘强制清仓
    """
    author = "Apollo-AI-Trader"

    parameters = BaseStrategy.parameters + [
        "volume_multiplier",
        "imbalance_threshold",
        "profit_activation_pct",
        "profit_rollback_pct",
        "ai_score",
        "backtest_win_loss_ratio",
        "sentiment_index",
        "tick_cooldown_ms",
        "use_regime_sizing",
    ]
    variables = BaseStrategy.variables + [
        "long_pos", "last_price",
        "current_trend", "stop_line",
        "trailing_profit_active", "highest_price_since_entry", "profit_target_line",
    ]

    DEFAULTS = {
        **BaseStrategy.DEFAULTS,   # ★ 修复
        "volume_multiplier": 2.5,
        "imbalance_threshold": 0.65,
        "profit_activation_pct": 0.020,
        "profit_rollback_pct": 0.005,
        "ai_score": 0.70,
        "backtest_win_loss_ratio": 1.8,
        "sentiment_index": 0.50,
        "tick_cooldown_ms": 500,
        "use_regime_sizing": True,
        # 覆盖基类默认值
        "stop_loss_pct": 0.008,
        "max_position_pct": 0.08,
    }

    # 盘口权重（指数衰减）
    BOOK_WEIGHTS = (0.40, 0.25, 0.15, 0.12, 0.08)

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.need_tick = True

        self.long_pos = 0
        self.last_price = 0.0
        self.current_trend = 1
        self.stop_line = 0.0
        self.trailing_profit_active = False
        self.highest_price_since_entry = 0.0
        self.profit_target_line = 0.0

        self.is_us_market = self.is_us
        self.market_tz = _US_TZ if self.is_us else _HK_TZ

        self._last_tick_time = 0.0
        self._last_imbalance = 0.5

        self.write_log(f"[INIT] 订单流策略 | US={self.is_us_market} threshold={self.imbalance_threshold}")

    def on_init(self):
        super().on_init()

    def on_start(self):
        super().on_start()
        self.trailing_profit_active = False
        self.highest_price_since_entry = 0.0

    # ───────────────────────────
    #  时间窗口
    # ───────────────────────────
    def check_time_window(self, now_t: Optional[dtime] = None) -> Tuple[bool, bool]:
        if now_t is None:
            now_t = datetime.now(self.market_tz).time()
        if self.is_us_market:
            allow_open = dtime(9, 45) <= now_t < dtime(15, 50)
            must_close = now_t >= dtime(15, 50)
        else:
            allow_am = dtime(9, 45) <= now_t < dtime(11, 55)
            allow_pm = dtime(13, 15) <= now_t < dtime(15, 50)
            allow_open = allow_am or allow_pm
            must_close = (dtime(11, 55) <= now_t < dtime(12, 0)) or (now_t >= dtime(15, 50))
        return allow_open, must_close

    # ───────────────────────────
    #  Kelly 仓位（结合 Regime）
    # ───────────────────────────
    def calculate_kelly_volume(self, current_price: float) -> int:
        p = self.ai_score
        q = 1.0 - p
        b = max(self.backtest_win_loss_ratio, 0.1)
        if b <= 0 or current_price <= 0:
            return self.fixed_size

        f_star = (p - (q / b)) * 0.5
        f_optimized = min(max(f_star * (0.5 + self.sentiment_index), 0.01), self.max_position_pct)

        # Regime 缩放
        if self.use_regime_sizing:
            r = self.get_current_regime()
            if r in ("strong_bull", "strong_bear"):
                f_optimized *= 1.3
            elif r == "unknown":
                f_optimized *= 0.5

        if self.is_us_market:
            available = 50000.0
            lot = 1
        else:
            available = 810000.0
            lot = 100

        raw = (available * f_optimized / current_price) / lot
        return int(max(round(raw) * lot, lot))

    # ───────────────────────────
    #  on_tick（核心）
    #  v2.9.6：不再调用 super().on_tick()
    #  原因：need_tick=True 时基类 on_tick 会执行完整逻辑
    #  （包括 bg_1m.update_tick），导致同一 tick 被喂入两次。
    #  本策略完全自己处理 Tick，不依赖基类的 Tick→Bar 合成。
    # ───────────────────────────
    def on_tick(self, tick: TickData):
        """Tick 级微观执行：这是本策略的主战场"""
        if not self.need_tick:
            return

        if not tick or tick.last_price <= 0:
            return

        self.last_price = tick.last_price

        # 冷却期（防止同一 tick 风暴重复触发）
        now_ms = time.time() * 1000
        if now_ms - self._last_tick_time < self.tick_cooldown_ms:
            return
        self._last_tick_time = now_ms

        # 持仓管理（优先于开仓）
        if self.long_pos > 0:
            self._manage_position(tick)
            return

        # 空仓：判断是否开仓
        allow_open, must_close = self.check_time_window()
        if not allow_open:
            return
        if self.sentiment_index < 0.45 and self.get_current_regime() == "unknown":
            return

        imbalance = self._calc_imbalance(tick)
        self._last_imbalance = imbalance

        if imbalance > self.imbalance_threshold:
            self.is_ordering = True
            vol = self.calculate_kelly_volume(tick.last_price)
            self.current_trend = 1
            self.write_log(f"🟢 AI开仓 | imb={imbalance:.2f} vol={vol} price={tick.last_price:.2f}")
            if self.notice_callback:
                self.notice_callback(self.vt_symbol, tick.last_price, "🟢 AI开仓", f"imb={imbalance:.2f}")
            self.buy(tick.last_price + 0.05, vol)

    # ───────────────────────────
    #  盘口 imbalance
    # ───────────────────────────
    def _calc_imbalance(self, tick: TickData) -> float:
        """加权盘口 imbalance，防御性降级"""
        try:
            bids = [
                getattr(tick, 'bid_volume_1', 0) or 0,
                getattr(tick, 'bid_volume_2', 0) or 0,
                getattr(tick, 'bid_volume_3', 0) or 0,
                getattr(tick, 'bid_volume_4', 0) or 0,
                getattr(tick, 'bid_volume_5', 0) or 0,
            ]
            asks = [
                getattr(tick, 'ask_volume_1', 0) or 0,
                getattr(tick, 'ask_volume_2', 0) or 0,
                getattr(tick, 'ask_volume_3', 0) or 0,
                getattr(tick, 'ask_volume_4', 0) or 0,
                getattr(tick, 'ask_volume_5', 0) or 0,
            ]
            total_bid = sum(b * w for b, w in zip(bids, self.BOOK_WEIGHTS) if b > 0)
            total_ask = sum(a * w for a, w in zip(asks, self.BOOK_WEIGHTS) if a > 0)
            total = total_bid + total_ask
            if total <= 0:
                return 0.5
            return total_bid / total
        except (AttributeError, TypeError):
            # 降级：仅 1 档
            b1 = getattr(tick, 'bid_volume_1', 0) or 0
            a1 = getattr(tick, 'ask_volume_1', 0) or 0
            total = b1 + a1
            if total <= 0:
                return 0.5
            return b1 / total

    # ───────────────────────────
    #  持仓管理
    # ───────────────────────────
    def _manage_position(self, tick: TickData):
        price = tick.last_price

        # 更新最高价
        if price > self.highest_price_since_entry:
            self.highest_price_since_entry = price

        # 激活移动止盈
        if not self.trailing_profit_active:
            if price >= self.entry_price * (1 + self.profit_activation_pct):
                self.trailing_profit_active = True
                self.profit_target_line = price * (1 - self.profit_rollback_pct)
                self.write_log(f"🎯 移动止盈激活 @ {self.profit_target_line:.2f}")
        else:
            new_line = price * (1 - self.profit_rollback_pct)
            if new_line > self.profit_target_line:
                self.profit_target_line = new_line

        # 移动止盈触发
        if self.trailing_profit_active and price <= self.profit_target_line:
            self.is_ordering = True
            self.write_log(f"💰 追踪止盈 @ {price:.2f} < {self.profit_target_line:.2f}")
            if self.notice_callback:
                self.notice_callback(self.vt_symbol, price, "💰 追踪止盈", f"跌破 {self.profit_target_line:.2f}")
            self.sell(price - 0.05, self.long_pos)
            return

        # 硬止损
        if price <= self.stop_line:
            self.is_ordering = True
            self.write_log(f"🛡️ 止损 @ {price:.2f} <= {self.stop_line:.2f}")
            if self.notice_callback:
                self.notice_callback(self.vt_symbol, price, "🛡️ 止损", f"跌破 {self.stop_line:.2f}")
            self.sell(price - 0.05, self.long_pos)
            return

        # 尾盘强制清仓
        _, must_close = self.check_time_window()
        if must_close:
            self.is_ordering = True
            self.write_log(f"🛑 尾盘清仓")
            if self.notice_callback:
                self.notice_callback(self.vt_symbol, price, "🛑 尾盘清仓", "")
            self.sell(price - 0.05, self.long_pos)

    # ───────────────────────────
    #  on_bar（备用，记录 1M 收盘价）
    # ───────────────────────────
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        self.last_price = bar.close_price

    # ───────────────────────────
    #  回调
    # ───────────────────────────
    def on_order(self, order: OrderData):
        super().on_order(order)
        if order.status in (Status.REJECTED, Status.CANCELLED, Status.ALLTRADED):
            self.is_ordering = False

    def on_trade(self, trade: TradeData):
        super().on_trade(trade)
        if trade.direction == Direction.LONG:
            self.long_pos += trade.volume
            self.entry_price = trade.price
            self.stop_line = round(trade.price * (1 - self.stop_loss_pct), 3)
            self.highest_price_since_entry = trade.price
            self.trailing_profit_active = False
            self.write_log(f"[成交] BUY {trade.volume}@{trade.price:.2f} | 持仓={self.long_pos}")
        else:
            pnl = (trade.price - self.entry_price) * trade.volume if self.entry_price > 0 else 0
            self.today_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1

            # 部分平仓时正确维护状态
            self.long_pos = max(0, self.long_pos - trade.volume)
            if self.long_pos == 0:
                self.entry_price = 0.0
                self.stop_line = 0.0
                self.trailing_profit_active = False
                self.highest_price_since_entry = 0.0
                self.profit_target_line = 0.0
            else:
                self.write_log(f"[成交] 部分平仓 {trade.volume}@{trade.price:.2f} | 剩余={self.long_pos}")

            self.write_log(f"[成交] SELL {trade.volume}@{trade.price:.2f} | PnL={pnl:+.2f}")
        self.is_ordering = False
        self.put_event()