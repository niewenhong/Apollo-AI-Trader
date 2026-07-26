"""
strategies/equity/multi_indicator_strategy.py — Apollo-AI-Trader v2.8.0
MultiIndicatorStrategy 完整版 + 调试日志

v2.8.0 修正：
- 修复导入：从 vnpy_ctastrategy 导入 CtaTemplate（标准路径）
- 补全类型导入：BarData, TickData, OrderData, TradeData
- 补全常量导入：Direction, Offset, Status, Exchange, Interval
- 补全工具导入：BarGenerator, ArrayManager
- 移除 try/except ImportError 假桩（打桩迟早出问题）
"""
import time
import numpy as np
from datetime import datetime
from typing import Optional

# ====== vnpy 标准导入（必须全部显式导入，不可省略）======
from vnpy.trader.constant import (
    Direction, Offset, Status, Interval, Exchange, OrderType
)
from vnpy.trader.object import (
    BarData, TickData, OrderData, TradeData, PositionData,
    AccountData, ContractData, LogData
)
from vnpy.trader.utility import (
    ArrayManager, BarGenerator, extract_vt_symbol, round_to, get_digits
)
from vnpy_ctastrategy import CtaTemplate, StopOrder


class MultiIndicatorStrategy(CtaTemplate):
    """
    10 维共振策略（完整版）
    使用富途实时推送的 bar（不合成），直接 on_bar 驱动
    """
    author = "Apollo-AI-Trader"

    parameters = [
        "fast_window", "slow_window", "rsi_window",
        "score_threshold", "sell_threshold", "fixed_size",
        "debug_auto_buy",
        "debug_auto_price",
    ]
    variables = [
        "pos", "today_pnl", "entry_price",
        "composite_score", "cumulative_pnl", "total_trades",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # ── 策略参数（带默认值）──
        self.fast_window = setting.get("fast_window", 5)
        self.slow_window = setting.get("slow_window", 20)
        self.rsi_window = setting.get("rsi_window", 14)
        self.score_threshold = setting.get("score_threshold", 5)
        self.sell_threshold = setting.get("sell_threshold", 2)
        self.fixed_size = setting.get("fixed_size", 100)

        # ── 调试开关（默认关闭）──
        self.debug_auto_buy = setting.get("debug_auto_buy", False)
        self.debug_auto_price = setting.get("debug_auto_price", 1.0)

        # ── 运行时状态 ──
        self.pos = 0
        self.today_pnl = 0.0
        self.entry_price = 0.0
        self.composite_score = 5
        self.cumulative_pnl = 0.0
        self.total_trades = 0
        self.active = False
        self.inited = False
        self.trading = False

        # ── 工具 ──
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager()

        # ── 订单追踪 ──
        self._debug_orders = {}
        self._last_traded = 0

        self.write_log(f"[INIT] {strategy_name} | {vt_symbol} | params loaded")

    # ──────────────────────────────
    #  生命周期
    # ──────────────────────────────
    def on_init(self):
        self.inited = True
        self.write_log(f"[on_init] ✅ 初始化完成 | pos={self.pos}")

    def on_start(self):
        self.active = True
        self.trading = True
        self.write_log(f"[on_start] ▶️ 策略已激活")

        if self.debug_auto_buy:
            self.write_log(f"[DEBUG] 自动发单测试 | price={self.debug_auto_price} vol={self.fixed_size}")
            try:
                vt = self.buy(self.debug_auto_price, self.fixed_size)
                self.write_log(f"[DEBUG] buy() 返回: {vt}")
            except Exception as e:
                self.write_log(f"[DEBUG] buy() 异常: {e}")

    def on_stop(self):
        self.active = False
        self.trading = False
        self.write_log(f"[on_stop] ⏸ 策略已停止 | pos={self.pos} pnl={self.today_pnl:.2f}")

    # ──────────────────────────────
    #  K线 / Tick
    # ──────────────────────────────
    def on_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # 打印 K 线
        self.write_log(
            f"[on_bar] {bar.datetime.strftime('%H:%M')} | "
            f"O={bar.open_price:.2f} H={bar.high_price:.2f} "
            f"L={bar.low_price:.2f} C={bar.close_price:.2f} "
            f"V={bar.volume}"
        )

        # 10 维共振评分（示例：快慢均线 + RSI）
        fast_ma = self.am.sma(self.fast_window)
        slow_ma = self.am.sma(self.slow_window)
        rsi = self.am.rsi(self.rsi_window)

        score = 5
        if fast_ma > slow_ma:
            score += 2
        if rsi > 60:
            score += 1
        if fast_ma < slow_ma:
            score -= 2
        if rsi < 40:
            score -= 1

        self.composite_score = score

        self.write_log(f"[score] composite={score} | fast_ma={fast_ma:.2f} slow_ma={slow_ma:.2f} rsi={rsi:.1f}")

        # 买入信号
        if score >= self.score_threshold and self.pos == 0:
            price = bar.close_price
            self.write_log(f"[SIGNAL] BUY | score={score}>={self.score_threshold} | price={price:.2f}")
            self.on_buy_signal(self.fixed_size)

        # 卖出信号
        elif score <= self.sell_threshold and self.pos > 0:
            price = bar.close_price
            self.write_log(f"[SIGNAL] SELL | score={score}<={self.sell_threshold} | price={price:.2f}")
            self.on_sell_signal(self.fixed_size)

    def on_tick(self, tick: TickData):
        """直接使用富途推送的 tick，不合成 bar"""
        self.bg.update_tick(tick)

    # ──────────────────────────────
    #  下单 / 撤单
    # ──────────────────────────────
    def on_buy_signal(self, size=0):
        if not self.trading or self.pos > 0:
            self.write_log(f"[on_buy_signal] SKIP | trading={self.trading} pos={self.pos}")
            return
        sz = size if size > 0 else self.fixed_size
        price = self.am.close[-1] if self.am.inited else 1.0
        self.write_log(f"[on_buy_signal] BUY | size={sz} price={price:.2f}")
        try:
            vt = self.buy(price, sz, stop=False, lock=False)
            self.write_log(f"[on_buy_signal] buy() returned: {vt}")
        except Exception as e:
            self.write_log(f"[on_buy_signal] buy() 异常: {e}")

    def on_sell_signal(self, size=0):
        if not self.trading or self.pos <= 0:
            self.write_log(f"[on_sell_signal] SKIP | trading={self.trading} pos={self.pos}")
            return
        sz = size if size > 0 else self.fixed_size
        price = self.am.close[-1] if self.am.inited else 1.0
        self.write_log(f"[on_sell_signal] SELL | size={sz} price={price:.2f}")
        try:
            vt = self.sell(price, sz, stop=False, lock=False)
            self.write_log(f"[on_sell_signal] sell() returned: {vt}")
        except Exception as e:
            self.write_log(f"[on_sell_signal] sell() 异常: {e}")

    def cancel_all(self):
        active = list(self._debug_orders.keys())
        self.write_log(f"[cancel_all] 触发 | active_orders={active}")
        super().cancel_all()

    # ──────────────────────────────
    #  回调
    # ──────────────────────────────
    def on_order(self, order: OrderData):
        vt = order.vt_orderid
        st = order.status
        traded = getattr(order, 'traded', 0)
        volume = getattr(order, 'volume', 0)

        self.write_log(
            f"[on_order] {vt} | status={st.name if hasattr(st,'name') else st} | "
            f"traded={traded}/{volume} | price={order.price}"
        )

        if vt not in self._debug_orders:
            self._debug_orders[vt] = {"submit_time": time.time(), "status": str(st)}
        self._debug_orders[vt]["status"] = str(st)

        # 用 on_order.traded 更新 pos（模拟盘 on_trade 可能不走）
        if traded > 0:
            prev = getattr(self, '_last_traded', 0)
            delta = traded - prev
            if delta > 0:
                if order.direction == Direction.LONG:
                    self.pos += delta
                elif order.direction == Direction.SHORT:
                    self.pos -= delta
                self._last_traded = traded
                self.write_log(f"[on_order] pos 更新: +{delta} → pos={self.pos}")

        if st in (Status.ALLTRADED, Status.CANCELLED, Status.REJECTED):
            if vt in self._debug_orders:
                self._debug_orders[vt]["close_time"] = time.time()
                elapsed = self._debug_orders[vt]["close_time"] - self._debug_orders[vt]["submit_time"]
                self.write_log(f"[on_order] 终态 | {vt} | 耗时={elapsed:.1f}s")

    def on_trade(self, trade: TradeData):
        self.write_log(
            f"[on_trade] ✅ {trade.tradeid} | {trade.direction.name if hasattr(trade.direction,'name') else trade.direction} | "
            f"{trade.volume}@{trade.price:.2f}"
        )
        if trade.direction == Direction.LONG:
            self.pos += trade.volume
        elif trade.direction == Direction.SHORT:
            self.pos -= trade.volume
        self.total_trades += 1
        self.write_log(f"[on_trade] pos={self.pos} total_trades={self.total_trades}")

    # ──────────────────────────────
    #  调试辅助
    # ──────────────────────────────
    def show_debug_orders(self) -> str:
        if not self._debug_orders:
            return "📋 无订单记录"
        lines = [f"📋 [{self.strategy_name}] 订单追踪:"]
        for vt, info in self._debug_orders.items():
            lines.append(f"  {vt}: {info}")
        return "\n".join(lines)
