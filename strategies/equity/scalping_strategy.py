# -*- coding: utf-8 -*-
"""
strategies/equity/scalping_strategy.py - v3.3.1
剥头皮高频交易策略 - 继承 BaseStrategy
基于 Tick 级盘口数据，捕捉微小价格失衡，快速开平仓。
"""
import time
import logging
from collections import deque
from datetime import datetime, time as dtime
from typing import Optional, Tuple

from vnpy.trader.constant import Direction, Status
from vnpy.trader.object import BarData, TickData, TradeData, OrderData

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("ScalpingStrategy")


class ScalpingStrategy(BaseStrategy):
    """剥头皮策略（v3.3.1）"""

    author = "Apollo AI Trader"

    parameters = BaseStrategy.parameters + [
        "min_spread",               # 最小买卖价差
        "order_book_depth",         # 盘口深度
        "imbalance_ratio",          # 盘口不平衡阈值
        "take_profit_pct",          # 止盈百分比
        "stop_loss_pct",            # 止损百分比
        "max_position_seconds",     # 最大持仓时间（秒）
        "volume_spike_multiplier",  # 量比阈值
        "vwap_deviation_pct",       # VWAP偏离百分比
        "fixed_size",               # 每笔交易固定手数
        "max_pos",                  # 最大净持仓
        "tick_cooldown_ms",         # Tick冷却时间（毫秒）
    ]
    variables = BaseStrategy.variables + [
        "vwap", "deviation_val",
        "bid_volumes", "ask_volumes",
        "last_minute_volume", "current_minute_volume",
        "entry_time", "entry_price",
        "position_direction",
    ]

    DEFAULTS = {
        **BaseStrategy.DEFAULTS,
        "min_spread": 0.01,
        "order_book_depth": 5,
        "imbalance_ratio": 1.5,
        "take_profit_pct": 0.001,
        "stop_loss_pct": 0.002,
        "max_position_seconds": 120,
        "volume_spike_multiplier": 3.0,
        "vwap_deviation_pct": 0.002,
        "fixed_size": 100,
        "max_pos": 200,
        "tick_cooldown_ms": 500,
    }

    # 盘口权重（指数衰减）
    BOOK_WEIGHTS = (0.40, 0.25, 0.15, 0.12, 0.08)

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.need_tick = True

        self.vwap = 0.0
        self.deviation_val = 0.0
        self.bid_volumes = [0] * self.order_book_depth
        self.ask_volumes = [0] * self.order_book_depth
        self.last_minute_volume = 0
        self.current_minute_volume = 0
        self.entry_time = None
        self.entry_price = 0.0
        self.position_direction = None

        self._last_tick_time = 0.0
        self._last_minute_mark = None
        self._prev_volume = 0
        self._prev_tick_volume = 0
        self._vwap_numer = 0.0
        self._vwap_denom = 0.0

        self.write_log(f"[INIT] Scalping | imbalance={self.imbalance_ratio} tp={self.take_profit_pct} sl={self.stop_loss_pct}")

    def on_init(self):
        super().on_init()
        self.write_log("ScalpingStrategy 初始化完成")

    def on_start(self):
        super().on_start()
        self._last_minute_mark = datetime.now()
        self.write_log("ScalpingStrategy 启动")

    def on_stop(self):
        super().on_stop()
        self.write_log("ScalpingStrategy 停止")

    # ───────────────────────────
    #  时间窗口（复用基类）
    # ───────────────────────────
    def check_time_window(self, now_t: Optional[dtime] = None) -> Tuple[bool, bool]:
        if now_t is None:
            now_t = datetime.now(self.market_tz).time() if hasattr(self, 'market_tz') else datetime.now().time()
        if self.is_us:
            allow_open = dtime(9, 45) <= now_t < dtime(15, 50)
            must_close = now_t >= dtime(15, 50)
        else:
            allow_am = dtime(9, 45) <= now_t < dtime(11, 55)
            allow_pm = dtime(13, 15) <= now_t < dtime(15, 50)
            allow_open = allow_am or allow_pm
            must_close = (dtime(11, 55) <= now_t < dtime(12, 0)) or (now_t >= dtime(15, 50))
        return allow_open, must_close

    # ───────────────────────────
    #  on_tick（核心）
    # ───────────────────────────
    def on_tick(self, tick: TickData):
        if not self.need_tick or not tick or tick.last_price <= 0:
            return

        # 冷却
        now_ms = time.time() * 1000
        if now_ms - self._last_tick_time < self.tick_cooldown_ms:
            return
        self._last_tick_time = now_ms

        # 更新数据
        self._update_order_book(tick)
        self._update_volume(tick)
        self._update_vwap(tick)

        # 持仓管理
        if self.pos != 0:
            self._check_exit(tick)
            return

        # 空仓：检测入场信号
        allow_open, must_close = self.check_time_window()
        if not allow_open:
            return
        if not self.is_regime_tradeable():
            return
        if not getattr(self, '_trading_allowed', False):
            return

        self._check_entry_signals(tick)

    # ───────────────────────────
    #  数据更新
    # ───────────────────────────
    def _update_order_book(self, tick: TickData):
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
            depth = self.order_book_depth
            self.bid_volumes = [bids[i] for i in range(depth)]
            self.ask_volumes = [asks[i] for i in range(depth)]
        except (AttributeError, IndexError):
            # 降级：仅1档
            self.bid_volumes = [getattr(tick, 'bid_volume_1', 0) or 0]
            self.ask_volumes = [getattr(tick, 'ask_volume_1', 0) or 0]

    def _update_volume(self, tick: TickData):
        now = datetime.now()
        if self._last_minute_mark is None:
            self._last_minute_mark = now
            return

        elapsed = (now - self._last_minute_mark).seconds
        if elapsed >= 60:
            self.last_minute_volume = self.current_minute_volume
            self.current_minute_volume = 0
            self._last_minute_mark = now
        else:
            delta = tick.volume - self._prev_volume
            if delta > 0:
                self.current_minute_volume += delta
        self._prev_volume = tick.volume

    def _update_vwap(self, tick: TickData):
        price = tick.last_price or tick.ask_price_1 or tick.bid_price_1
        delta = tick.volume - self._prev_tick_volume
        if delta > 0 and price > 0:
            self._vwap_numer += price * delta
            self._vwap_denom += delta
            if self._vwap_denom > 0:
                self.vwap = self._vwap_numer / self._vwap_denom
        self._prev_tick_volume = tick.volume

    # ───────────────────────────
    #  入场信号检测
    # ───────────────────────────
    def _check_entry_signals(self, tick: TickData):
        signals = []

        # 1. 盘口不平衡
        total_bid = sum(self.bid_volumes)
        total_ask = sum(self.ask_volumes)
        if total_bid > 0 and total_ask > 0:
            ratio = total_bid / total_ask
            if ratio >= self.imbalance_ratio:
                signals.append(("imbalance_long", f"买盘强 ratio={ratio:.2f}"))
            elif ratio <= 1.0 / self.imbalance_ratio:
                signals.append(("imbalance_short", f"卖盘强 ratio={ratio:.2f}"))

        # 2. 价差极窄
        spread = (tick.ask_price_1 - tick.bid_price_1) if tick.ask_price_1 and tick.bid_price_1 else 0
        if 0 < spread < self.min_spread * 2:
            signals.append(("tight_spread", f"价差窄 {spread:.3f}"))

        # 3. 成交量突增
        if self.last_minute_volume > 0 and self.current_minute_volume > self.last_minute_volume * self.volume_spike_multiplier:
            signals.append(("volume_spike", f"量比 {self.current_minute_volume/self.last_minute_volume:.1f}x"))

        # 4. VWAP偏离
        if self.vwap > 0 and tick.last_price:
            deviation = (tick.last_price - self.vwap) / self.vwap
            if abs(deviation) >= self.vwap_deviation_pct:
                direction = "long" if deviation < 0 else "short"
                signals.append((f"vwap_deviation_{direction}", f"VWAP偏离 {deviation*100:.2f}%"))

        # 5. 大单吃单
        delta = tick.volume - self._prev_tick_volume
        if delta > 500:
            signals.append(("big_trade", f"大单 {delta}股"))

        # 执行开仓（取第一个有效信号）
        for sig_type, reason in signals:
            if "long" in sig_type or sig_type == "imbalance_long":
                if self.pos >= self.max_pos:
                    continue
                self._open_position(Direction.LONG, tick.last_price, reason)
                return
            elif "short" in sig_type or sig_type == "imbalance_short":
                if self.pos <= -self.max_pos:
                    continue
                self._open_position(Direction.SHORT, tick.last_price, reason)
                return

    def _open_position(self, direction: Direction, price: float, reason: str):
        if direction == Direction.LONG:
            self.buy(price, self.fixed_size)
            self.write_log(f"🟢 开多 {self.fixed_size}股 @ {price:.2f} - {reason}")
        else:
            self.short(price, self.fixed_size)
            self.write_log(f"🔴 开空 {self.fixed_size}股 @ {price:.2f} - {reason}")

    # ───────────────────────────
    #  持仓管理（止盈止损+超时）
    # ───────────────────────────
    def _check_exit(self, tick: TickData):
        if not self.entry_time or not tick.last_price:
            return

        price = tick.last_price

        # 止盈止损
        if self.pos > 0:
            pnl_pct = (price - self.entry_price) / self.entry_price
            if pnl_pct >= self.take_profit_pct:
                self.sell(price, abs(self.pos))
                self.write_log(f"💰 止盈平多 @ {price:.2f} (+{pnl_pct*100:.2f}%)")
                return
            elif pnl_pct <= -self.stop_loss_pct:
                self.sell(price, abs(self.pos))
                self.write_log(f"🛡️ 止损平多 @ {price:.2f} ({pnl_pct*100:.2f}%)")
                return
        elif self.pos < 0:
            pnl_pct = (self.entry_price - price) / self.entry_price
            if pnl_pct >= self.take_profit_pct:
                self.cover(price, abs(self.pos))
                self.write_log(f"💰 止盈平空 @ {price:.2f} (+{pnl_pct*100:.2f}%)")
                return
            elif pnl_pct <= -self.stop_loss_pct:
                self.cover(price, abs(self.pos))
                self.write_log(f"🛡️ 止损平空 @ {price:.2f} ({pnl_pct*100:.2f}%)")
                return

        # 超时平仓
        elapsed = (datetime.now() - self.entry_time).seconds
        if elapsed >= self.max_position_seconds:
            if self.pos > 0:
                self.sell(price, abs(self.pos))
                self.write_log(f"⏰ 超时平多 @ {price:.2f} ({elapsed}s)")
            else:
                self.cover(price, abs(self.pos))
                self.write_log(f"⏰ 超时平空 @ {price:.2f} ({elapsed}s)")

        # 尾盘强制清仓（复用基类逻辑，但这里显式处理）
        _, must_close = self.check_time_window()
        if must_close and self.pos != 0:
            if self.pos > 0:
                self.sell(price, abs(self.pos))
            else:
                self.cover(price, abs(self.pos))
            self.write_log(f"🛑 尾盘清仓 @ {price:.2f}")

    # ───────────────────────────
    #  on_bar（备用，记录1M收盘价）
    # ───────────────────────────
    def on_1m_bar(self, bar: BarData):
        super().on_1m_bar(bar)
        # 可在此更新VWAP（如果on_tick未及时更新）
        if self._vwap_denom > 0:
            self.vwap = self._vwap_numer / self._vwap_denom

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
            self.entry_price = trade.price
            self.entry_time = datetime.now()
            self.position_direction = Direction.LONG
            self.write_log(f"💹 成交多 {trade.volume}@{trade.price:.2f}")
        else:
            self.entry_price = trade.price
            self.entry_time = datetime.now()
            self.position_direction = Direction.SHORT
            self.write_log(f"💹 成交空 {trade.volume}@{trade.price:.2f}")
        self.put_event()