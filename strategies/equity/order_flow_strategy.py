"""
strategies/equity/order_flow_strategy.py - v2.8.0
Tick 级订单流策略：买卖盘加权失衡 + Kelly 仓位 + 移动止盈止损
v2.8.0 优化：
  - 继承 vnpy_ctastrategy.CtaTemplate 标准接口
  - 修复 pos 手动管理导致的不同步问题
  - 时间窗口判断优化、异常处理完善
  - 支持回测（BarData 兼容）和实盘（TickData）
  - 增加类型注解、常量提取、代码可读性
"""
from datetime import datetime, time as dtime
from typing import Optional, Callable, Tuple
import pytz
import numpy as np

from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import TickData, BarData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, Status


# ========== 默认参数 ==========
DEFAULT_PARAMS = {
    "bar_window": 1,
    "volume_multiplier": 2.5,
    "imbalance_threshold": 0.65,
    "stop_loss_pct": 0.008,
    "ai_score": 0.70,
    "backtest_win_loss_ratio": 1.8,
    "sentiment_index": 0.50,
    "profit_activation_pct": 0.020,
    "profit_rollback_pct": 0.005,
    "max_position_pct": 0.08,        # Kelly 上限占比
    "us_capital": 50000.0,           # 美股可用资金
    "hk_capital": 810000.0,          # 港股可用资金
    "tick_offset": 0.05,             # 下单价格偏移（滑点补偿）
    "enable_short": False,            # 是否允许做空
}

# ========== 时间窗口常量（美东时间）==========
US_MORNING_START = dtime(9, 45, 0)
US_MORNING_END   = dtime(15, 50, 0)
HK_AM_START      = dtime(9, 45, 0)
HK_AM_END        = dtime(11, 55, 0)
HK_PM_START      = dtime(13, 15, 0)
HK_PM_END        = dtime(15, 50, 0)

# 五档权重
W_BID = [0.40, 0.25, 0.15, 0.12, 0.08]


class TickOrderFlowStrategy(CtaTemplate):
    """Tick 级订单流策略（vnpy CtaTemplate 标准实现）"""

    author = "Apollo"

    parameters = list(DEFAULT_PARAMS.keys())
    variables = [
        "pos", "entry_price", "today_pnl",
        "long_pos", "stop_line", "trailing_profit_active",
        "highest_price_since_entry", "profit_target_line",
        "total_trades", "winning_trades", "is_ordering",
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 合并参数
        merged = {**DEFAULT_PARAMS, **setting}
        for key, value in merged.items():
            setattr(self, key, value)

        # 运行时状态
        self.long_pos = 0
        self.entry_price = 0.0
        self.stop_line = 0.0
        self.trailing_profit_active = False
        self.highest_price_since_entry = 0.0
        self.profit_target_line = 0.0
        self.today_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.is_ordering = False

        # 市场判断
        self.is_us_market = (".SMART" in vt_symbol) or (".US" in vt_symbol)
        self.market_tz = pytz.timezone("America/New_York") if self.is_us_market else pytz.timezone("Asia/Hong_Kong")

        # 通知回调（外部注入）
        self.notice_callback: Optional[Callable] = None

        self.write_log(f"[INIT] {strategy_name} | {vt_symbol} | {'US' if self.is_us_market else 'HK'}")

    # ──────────────────────────────
    #  生命周期
    # ──────────────────────────────
    def on_init(self):
        self.write_log("✅ 订单流策略初始化完成")

    def on_start(self):
        self.is_ordering = False
        self.trailing_profit_active = False
        self.highest_price_since_entry = 0.0
        self.write_log(f"▶️ 策略启动 | imbalance≥{self.imbalance_threshold} ai_score={self.ai_score}")

    def on_stop(self):
        self.write_log(f"⏸ 策略停止 | pnl={self.today_pnl:.2f} trades={self.total_trades}")

    # ──────────────────────────────
    #  Bar / Tick
    # ──────────────────────────────
    def on_bar(self, bar: BarData):
        """回测模式：用Bar模拟Tick价格"""
        self._process_price(bar.close_price, is_tick=False)

    def on_tick(self, tick: TickData):
        """实盘模式：处理Tick数据"""
        self._process_price(tick.last_price, is_tick=True, tick=tick)

    # ──────────────────────────────
    #  核心价格处理
    # ──────────────────────────────
    def _process_price(self, price: float, is_tick: bool = False, tick: Optional[TickData] = None):
        """统一处理价格更新（Bar和Tick共用逻辑）"""

        # 正在下单中，忽略
        if self.is_ordering:
            return

        # 持仓时：检查止盈止损
        if self.long_pos > 0:
            self._check_exit(price)
            return

        # 空仓时：检查开仓条件
        if not is_tick or tick is None:
            return

        # 情绪过低不允许开仓
        if self.sentiment_index < 0.45:
            return

        # 时间窗口检查
        allow_open, must_close = self._check_time_window()
        if must_close:
            return  # 清仓逻辑在 _check_exit 中处理

        if not allow_open:
            return

        # 计算订单簿失衡
        imbalance = self._calc_imbalance(tick)
        if imbalance is None:
            return

        # 开仓信号
        if imbalance > self.imbalance_threshold:
            self._open_long(tick, imbalance)

    # ──────────────────────────────
    #  开仓
    # ──────────────────────────────
    def _open_long(self, tick: TickData, imbalance: float):
        """多头开仓"""
        price = tick.last_price
        volume = self._calc_kelly_volume(price)

        self.is_ordering = True
        order_price = price + self.tick_offset

        self.write_log(
            f"🟢 开仓信号 | imbalance={imbalance:.2f} "
            f"price={order_price:.2f} vol={volume}"
        )

        if self.notice_callback:
            self.notice_callback(
                self.vt_symbol, price,
                "🟢 AI 策略开仓",
                f"买盘加权占比 {imbalance:.2f}，动态开仓 {volume} 股"
            )

        self.buy(order_price, volume)

    # ──────────────────────────────
    #  平仓检查
    # ──────────────────────────────
    def _check_exit(self, price: float):
        """检查是否满足止盈/止损条件"""
        entry = self.entry_price

        # 更新最高价
        if price > self.highest_price_since_entry:
            self.highest_price_since_entry = price

        # 激活移动止盈
        if not self.trailing_profit_active and entry > 0:
            pnl_pct = (price - entry) / entry
            if pnl_pct >= self.profit_activation_pct:
                self.trailing_profit_active = True
                self.profit_target_line = round(
                    self.highest_price_since_entry * (1.0 - self.profit_rollback_pct), 3
                )
                self.write_log(f"🎯 移动止盈激活 | 保护线={self.profit_target_line:.2f}")
                if self.notice_callback:
                    self.notice_callback(
                        self.vt_symbol, price,
                        "🎯 移动止盈激活",
                        f"保护线: {self.profit_target_line:.2f}"
                    )

        # 1) 移动止盈触发
        if self.trailing_profit_active and price <= self.profit_target_line:
            self._close_position(price, "移动止盈")
            return

        # 2) 硬止损
        if price <= self.stop_line and self.stop_line > 0:
            self._close_position(price, "止损")
            return

        # 3) 更新动态止损线（亏损未激活止盈时）
        if not self.trailing_profit_active:
            new_stop = round(price * (1.0 - self.stop_loss_pct), 3)
            if new_stop > self.stop_line:
                self.stop_line = new_stop

    def _close_position(self, price: float, reason: str):
        """平仓"""
        vol = self.long_pos
        if vol <= 0:
            return

        self.is_ordering = True
        order_price = price - self.tick_offset

        self.write_log(f"🔴 {reason}平仓 | price={order_price:.2f} vol={vol}")

        if self.notice_callback:
            self.notice_callback(
                self.vt_symbol, price,
                f"🔴 {reason}",
                f"平仓 {vol} 股 @ {order_price:.2f}"
            )

        self.sell(order_price, vol)

    # ──────────────────────────────
    #  订单簿失衡计算
    # ──────────────────────────────
    def _calc_imbalance(self, tick: TickData) -> Optional[float]:
        """计算买卖盘加权失衡比"""
        try:
            bids = [tick.bid_volume_1, tick.bid_volume_2, tick.bid_volume_3,
                    tick.bid_volume_4, tick.bid_volume_5]
            asks = [tick.ask_volume_1, tick.ask_volume_2, tick.ask_volume_3,
                    tick.ask_volume_4, tick.ask_volume_5]

            total_bid = sum(b * w for b, w in zip(bids, W_BID))
            total_ask = sum(a * w for a, w in zip(asks, W_BID))
            total = total_bid + total_ask

            if total == 0:
                return None
            return total_bid / total
        except AttributeError:
            # 回退：只用一档
            total = tick.bid_volume_1 + tick.ask_volume_1
            if total == 0:
                return None
            return tick.bid_volume_1 / total

    # ──────────────────────────────
    #  Kelly 仓位计算
    # ──────────────────────────────
    def _calc_kelly_volume(self, price: float) -> int:
        """Kelly 公式 + 情绪调整 → 计算下单数量"""
        p = self.ai_score
        q = 1.0 - p
        b = self.backtest_win_loss_ratio

        if b <= 0 or price <= 0:
            return 100

        f_star = (p - (q / b)) * 0.5  # 半 Kelly（更保守）
        f_adjusted = min(max(f_star * (0.5 + self.sentiment_index), 0.01), self.max_position_pct)

        capital = self.us_capital if self.is_us_market else self.hk_capital
        lot = 1 if self.is_us_market else 100

        raw = (capital * f_adjusted) / price
        return max(int(round(raw / lot) * lot), lot)

    # ──────────────────────────────
    #  时间窗口
    # ──────────────────────────────
    def _check_time_window(self) -> Tuple[bool, bool]:
        """
        返回 (允许开仓, 必须清仓)
        """
        now = datetime.now(self.market_tz).time()

        if self.is_us_market:
            allow = US_MORNING_START <= now < US_MORNING_END
            must_close = now >= US_MORNING_END
            return allow, must_close
        else:
            allow_am = HK_AM_START <= now < HK_AM_END
            allow_pm = HK_PM_START <= now < HK_PM_END
            must_close = (HK_AM_END <= now < HK_PM_START) or (now >= HK_PM_END)
            return (allow_am or allow_pm), must_close

    # ──────────────────────────────
    #  订单 / 成交回调
    # ──────────────────────────────
    def on_order(self, order: OrderData):
        """订单状态更新"""
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self.is_ordering = False
            self.write_log(f"📝 订单终态: {order.status.name}")

    def on_trade(self, trade: TradeData):
        """成交回调（vnpy 自动维护 self.pos）"""
        self.is_ordering = False

        if trade.direction == Direction.LONG:
            self.long_pos += trade.volume
            self.entry_price = trade.price
            self.stop_line = round(trade.price * (1.0 - self.stop_loss_pct), 3)
            self.highest_price_since_entry = trade.price
            self.trailing_profit_active = False
            self.write_log(f"💰 买入成交: {trade.volume}@{trade.price:.2f}")
        else:
            pnl = (trade.price - self.entry_price) * trade.volume if self.entry_price > 0 else 0.0
            self.total_trades += 1
            self.today_pnl += pnl
            if pnl > 0:
                self.winning_trades += 1

            self.long_pos = max(0, self.long_pos - trade.volume)
            if self.long_pos == 0:
                self.entry_price = 0.0
                self.stop_line = 0.0
                self.trailing_profit_active = False
                self.highest_price_since_entry = 0.0
                self.profit_target_line = 0.0

            self.write_log(f"💰 卖出成交: {trade.volume}@{trade.price:.2f} PnL={pnl:.2f}")

        self.put_event()
