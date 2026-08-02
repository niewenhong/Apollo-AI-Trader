"""
strategies/ipo/ipo_strategy.py - Apollo-AI-Trader v2.9.7
IPO策略：新股申购 + 首日交易
- 申购筛选：估值/行业/绿鞋/基石
- 首日：开盘观察 → 突破入场 → 止盈止损

v2.9.7 变更：
- 继承 ApolloBaseStrategy（统一 Tick/Bar 管理、AI 参数、Regime 感知）
- 删除独立的 BarGenerator/ArrayManager（由基类提供 bg_1m/am）
- 修复 datetime.time() 比较错误
- 修复 _evaluate_entry 中 status 被错误重置为 "observing"
- 修复 on_order 中 pos 赋值错误（应为累加而非覆盖）
- 新增 need_tick=False（IPO 首日只需 1M Bar，不需 Tick）
- 新增 IPO 首日检测增强（价格突变 + 成交量爆发 + 新交易日）
- 使用单 class 定义，避免动态绑定导致 super() 失效
"""
import logging
from datetime import datetime, time as dtime
from typing import Optional

from vnpy.trader.object import BarData, TickData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, OrderType, Status

# 条件导入基类
try:
    from strategies.base_strategy import ApolloBaseStrategy
    _BASE_OK = True
except ImportError:
    try:
        from vnpy_ctastrategy import CtaTemplate as _CtaTemplate
    except ImportError:
        from vnpy.app.cta_strategy import CtaTemplate as _CtaTemplate
    _BASE_OK = False

logger = logging.getLogger("IPOStrategy")


if _BASE_OK:
    class IPOStrategy(ApolloBaseStrategy):
        """
        IPO 新股策略（继承 ApolloBaseStrategy）
        - 自动获得：Tick 管理、多周期 Bar 合成、AI 参数加载、
          Regime 感知、统一平仓/止盈/止损工具
        """
        author = "Apollo"
        version = "v2.9.7"

        # ── IPO 专属参数 ──
        min_subscribe_ratio = 50.0
        max_pe_ratio = 30.0
        require_greenshoe = True
        min_foundation_ratio = 0.20

        first_day_max_hold_min = 240
        profit_take_pct = 0.30
        stop_loss_pct = 0.15
        open_observe_min = 5
        breakout_threshold = 0.05
        max_capital_per_ipo = 50000

        fixed_size = 500
        is_simulate = True

        # 合并基类 parameters
        parameters = ApolloBaseStrategy.parameters + [
            "min_subscribe_ratio", "max_pe_ratio", "require_greenshoe",
            "min_foundation_ratio", "first_day_max_hold_min", "profit_take_pct",
            "stop_loss_pct", "open_observe_min", "breakout_threshold",
            "max_capital_per_ipo", "fixed_size", "is_simulate",
        ]
        variables = ApolloBaseStrategy.variables + [
            "entry_time", "highest", "lowest",
            "status", "ipos_tracked", "today_ipo",
        ]

        # ─────────────────────────────
        #  初始化
        # ─────────────────────────────
        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)

            # IPO 专属状态
            self.entry_price = 0.0
            self.entry_time = None
            self.highest = 0.0
            self.lowest = 999999.0
            self.status = "idle"  # idle/subscribed/observing/holding/closed
            self.ipos_tracked = 0
            self.today_ipo = False
            self._open_price = 0.0
            self._bars_since_open = 0
            self._last_bar_date = None

            # IPO 策略不需要 Tick 级数据
            self.need_tick = False

            self.write_log(
                f"[IPO] ✅ 初始化完成 | {vt_symbol} | "
                f"breakout={self.breakout_threshold*100:.0f}% "
                f"profit={self.profit_take_pct*100:.0f}% "
                f"stop={self.stop_loss_pct*100:.0f}%"
            )

        # ─────────────────────────────
        #  生命周期
        # ─────────────────────────────
        def on_init(self):
            self.write_log(f"[IPO] on_init | {self.vt_symbol}")

        def on_start(self):
            self.write_log(f"[IPO] ▶️ 启动 | 监控新股上市")
            self.status = "idle"
            self.today_ipo = False
            self._bars_since_open = 0

        def on_stop(self):
            self.write_log(
                f"[IPO] ⏸ 停止 | 状态={self.status} "
                f"交易={self.ipos_tracked} 持仓={self.pos}"
            )

        # ─────────────────────────────
        #  数据入口
        # ─────────────────────────────
        def on_tick(self, tick: TickData):
            """
            IPO 策略不需要 Tick 数据。
            基类 on_tick 在 need_tick=False 时自动快速返回。
            Bar 数据由 on_bar 接收引擎推送的 1M K 线。
            """
            return

        def on_bar(self, bar: BarData):
            """
            接收 1M Bar（来自基类 bg_1m 合成或引擎直接推送）
            """
            # 更新 ArrayManager
            self.am.update_bar(bar)
            if not self.am.inited:
                return

            # 检测是否为新股首日
            if not self.today_ipo:
                is_first = self._is_ipo_first_day(bar)
                if is_first:
                    self.write_log(
                        f"[IPO] 🆕 检测到新股首日: {self.vt_symbol} "
                        f"@ {bar.close_price:.2f}"
                    )
                    self.status = "observing"
                    self.today_ipo = True
                    self._open_price = bar.open_price
                    self._bars_since_open = 0
                    self.lowest = bar.low_price if bar.low_price > 0 else bar.close_price

            # 状态机
            if self.status == "observing":
                self._bars_since_open += 1
                if self._bars_since_open >= self.open_observe_min:
                    self._evaluate_entry(bar)
            elif self.status == "holding":
                self._manage_holding(bar)

            # 喂给更高周期 BG（基类架构）
            self.bg_5m.update_bar(bar)
            self.bg_15m.update_bar(bar)
            self.bg_60m.update_bar(bar)
            self.bg_daily.update_bar(bar)

        # ─────────────────────────────
        #  IPO 首日检测（增强版）
        # ─────────────────────────────
        def _is_ipo_first_day(self, bar: BarData) -> bool:
            """
            多重条件判断是否为 IPO 首日：
            1. 近期价格全为 0（停牌/未上市），突然出现价格
            2. 成交量爆发（>10倍近期均值）
            3. 新交易日首次出现有效价格
            """
            closes = self.am.close
            if len(closes) < 10:
                return False

            # 条件1：近期零值突变
            recent = closes[-20:] if len(closes) >= 20 else closes
            zero_count = sum(1 for c in recent if c == 0)
            price_appeared = (zero_count >= 10) and (bar.close_price > 0)

            # 条件2：成交量爆发
            volumes = self.am.volume
            valid_vols = [v for v in volumes[-30:] if v > 0]
            vol_burst = False
            if len(valid_vols) >= 5:
                avg_vol = sum(valid_vols[-5:]) / 5
                if avg_vol > 0 and bar.volume > avg_vol * 10:
                    vol_burst = True

            # 条件3：新交易日
            bar_date = None
            if hasattr(bar, 'datetime') and bar.datetime:
                try:
                    bar_date = bar.datetime.date()
                except AttributeError:
                    bar_date = str(bar.datetime)[:10]

            new_trading_day = (bar_date is not None and bar_date != self._last_bar_date)
            self._last_bar_date = bar_date or self._last_bar_date

            return price_appeared and (vol_burst or new_trading_day)

        # ─────────────────────────────
        #  入场评估
        # ─────────────────────────────
        def _evaluate_entry(self, bar: BarData):
            """观察期结束后评估是否入场"""
            if self._open_price <= 0:
                self.write_log(f"[IPO] ⚠️ 开盘价为0，继续观察")
                # 保持 observing 状态，不重置计数
                self._bars_since_open = 0
                return

            change = (bar.close_price - self._open_price) / self._open_price

            if change >= self.breakout_threshold:
                self._buy(bar.close_price)
                self.write_log(
                    f"[IPO] 🚀 突破入场: +{change*100:.1f}% @ {bar.close_price:.2f}"
                )
            elif change < -self.stop_loss_pct:
                self.write_log(
                    f"[IPO] 📉 开盘即破发 {change*100:.1f}%，放弃"
                )
                self.status = "closed"
                self.today_ipo = False
                self._reset_ipo_state()
            else:
                # 继续观察，不重置 status
                self.write_log(f"[IPO] 👀 继续观察: {change*100:.1f}%")

        # ─────────────────────────────
        #  买入
        # ─────────────────────────────
        def _buy(self, price: float):
            """买入（带资金上限控制）"""
            try:
                size = min(
                    self.fixed_size,
                    int(self.max_capital_per_ipo / max(price, 0.01))
                )
                if size <= 0:
                    self.write_log(f"[IPO] ⚠️ 计算股数={size}，价格异常 price={price}")
                    return
                vt_orderid = self.buy(price + 0.01, size)
                self.write_log(f"[IPO] ✅ 买入委托 {size}股 @ {price:.2f} | oid={vt_orderid}")
            except Exception as e:
                self.write_log(f"[IPO] ❌ 买入失败: {e}")

        # ─────────────────────────────
        #  持仓管理
        # ─────────────────────────────
        def _manage_holding(self, bar: BarData):
            """管理首日持仓：止盈/止损/超时"""
            if self.pos <= 0:
                return

            hi = bar.high_price if bar.high_price > 0 else bar.close_price
            lo = bar.low_price if bar.low_price > 0 else bar.close_price
            self.highest = max(self.highest, hi)
            self.lowest = min(self.lowest, lo)

            # 止盈
            if bar.close_price >= self.entry_price * (1 + self.profit_take_pct):
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(
                    f"[IPO] 🎯 止盈 +{self.profit_take_pct*100:.0f}% @ {bar.close_price:.2f}"
                )
                self.status = "closed"
                self._reset_ipo_state()
                return

            # 止损
            if bar.close_price <= self.entry_price * (1 - self.stop_loss_pct):
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(
                    f"[IPO] 🛑 止损 -{self.stop_loss_pct*100:.0f}% @ {bar.close_price:.2f}"
                )
                self.status = "closed"
                self._reset_ipo_state()
                return

            # 超时平仓（基于 bars_held，由基类 on_1m_bar 也可触发）
            if hasattr(self, 'bars_held') and self.bars_held >= self.first_day_max_hold_min:
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(
                    f"[IPO] ⏰ 超时平仓 {self.bars_held}分钟 @ {bar.close_price:.2f}"
                )
                self.status = "closed"
                self._reset_ipo_state()

        # ─────────────────────────────
        #  状态重置
        # ─────────────────────────────
        def _reset_ipo_state(self):
            """重置 IPO 状态，准备监控下一只新股"""
            self.today_ipo = False
            self._open_price = 0.0
            self._bars_since_open = 0
            self.highest = 0.0
            self.lowest = 999999.0
            self.entry_time = None

        # ─────────────────────────────
        #  订单/成交处理
        # ─────────────────────────────
        def on_order(self, order: OrderData):
            """订单回报"""
            if order.status in (Status.REJECTED, Status.CANCELLED):
                self.write_log(f"[IPO] 📝 订单取消/拒绝: {order.status.name}")
            elif order.status == Status.ALLTRADED:
                self.write_log(f"[IPO] ✅ 订单全部成交 @ {order.price:.2f}")
                if self.entry_time is None:
                    self.entry_time = datetime.now()
                    self.status = "holding"
            elif order.traded > 0:
                # 部分成交：累加 pos
                if order.direction == Direction.LONG:
                    self.pos = (self.pos or 0) + order.traded
                    if self.entry_price == 0:
                        self.entry_price = order.price
                        self.entry_time = datetime.now()
                        self.status = "holding"
                        self.highest = order.price
                    self.write_log(
                        f"[IPO] 📊 部分买入: +{order.traded}股 @ {order.price:.2f}"
                    )
            self.put_event()

        def on_trade(self, trade: TradeData):
            """成交回报（最可靠的持仓来源）"""
            if trade.direction == Direction.LONG:
                self.pos = (self.pos or 0) + trade.volume
                self.entry_price = trade.price
                self.entry_time = datetime.now()
                self.status = "holding"
                self.highest = trade.price
                self.lowest = trade.price
                self.ipos_tracked += 1
                self.write_log(f"[IPO] 💰 买入成交: {trade.volume}股 @ {trade.price:.2f}")
            elif trade.direction == Direction.SHORT:
                self.pos = (self.pos or 0) - trade.volume
                self.write_log(f"[IPO] 💰 卖出成交: {trade.volume}股 @ {trade.price:.2f}")
                if self.pos <= 0:
                    self.status = "closed"
                    self._reset_ipo_state()
            self.put_event()

else:
    # ─────────────────────────────
    #  Fallback：基类不可用时独立运行
    # ─────────────────────────────
    from vnpy_ctastrategy import CtaTemplate
    from vnpy.trader.utility import BarGenerator, ArrayManager

    class IPOStrategy(CtaTemplate):
        author = "Apollo"
        version = "v2.9.7-fallback"

        min_subscribe_ratio = 50.0
        max_pe_ratio = 30.0
        require_greenshoe = True
        min_foundation_ratio = 0.20
        first_day_max_hold_min = 240
        profit_take_pct = 0.30
        stop_loss_pct = 0.15
        open_observe_min = 5
        breakout_threshold = 0.05
        max_capital_per_ipo = 50000
        fixed_size = 500
        is_simulate = True

        parameters = [
            "min_subscribe_ratio", "max_pe_ratio", "require_greenshoe",
            "min_foundation_ratio", "first_day_max_hold_min", "profit_take_pct",
            "stop_loss_pct", "open_observe_min", "breakout_threshold",
            "max_capital_per_ipo", "fixed_size", "is_simulate",
        ]
        variables = [
            "pos", "entry_price", "entry_time", "highest", "lowest",
            "status", "ipos_tracked", "today_ipo",
        ]

        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)
            for k, v in setting.items():
                setattr(self, k, v)

            self.entry_price = 0.0
            self.entry_time = None
            self.highest = 0.0
            self.lowest = 999999.0
            self.status = "idle"
            self.ipos_tracked = 0
            self.today_ipo = False
            self._open_price = 0.0
            self._bars_since_open = 0
            self._last_bar_date = None
            self.need_tick = False
            self.pos = 0

            self.bg = BarGenerator(self.on_bar)
            self.am = ArrayManager(size=100)

            self.write_log(f"[IPO] ✅ Fallback 初始化 | {vt_symbol}")

        def on_init(self):
            self.write_log(f"[IPO] on_init | {self.vt_symbol}")

        def on_start(self):
            self.write_log(f"[IPO] ▶️ 启动")
            self.status = "idle"
            self.today_ipo = False

        def on_stop(self):
            self.write_log(f"[IPO] ⏸ 停止")

        def on_tick(self, tick):
            return  # 不需要 Tick

        def on_bar(self, bar):
            self.am.update_bar(bar)
            if not self.am.inited:
                return
            if not self.today_ipo:
                is_first = self._is_ipo_first_day(bar)
                if is_first:
                    self.write_log(f"[IPO] 🆕 首日: {bar.close_price:.2f}")
                    self.status = "observing"
                    self.today_ipo = True
                    self._open_price = bar.open_price
                    self._bars_since_open = 0
            if self.status == "observing":
                self._bars_since_open += 1
                if self._bars_since_open >= self.open_observe_min:
                    self._evaluate_entry(bar)
            elif self.status == "holding":
                self._manage_holding(bar)

        def _is_ipo_first_day(self, bar):
            closes = self.am.close
            if len(closes) < 10:
                return False
            recent = closes[-20:]
            zero_count = sum(1 for c in recent if c == 0)
            return (zero_count >= 10) and (bar.close_price > 0)

        def _evaluate_entry(self, bar):
            if self._open_price <= 0:
                self._bars_since_open = 0
                return
            change = (bar.close_price - self._open_price) / self._open_price
            if change >= self.breakout_threshold:
                self._buy(bar.close_price)
            elif change < -self.stop_loss_pct:
                self.write_log(f"[IPO] 📉 破发 {change*100:.1f}%")
                self.status = "closed"
                self.today_ipo = False
                self._reset_ipo_state()
            else:
                self.write_log(f"[IPO] 👀 {change*100:.1f}%")

        def _buy(self, price):
            try:
                size = min(self.fixed_size, int(self.max_capital_per_ipo / max(price, 0.01)))
                if size > 0:
                    self.buy(price + 0.01, size)
                    self.write_log(f"[IPO] ✅ 买入 {size}股 @ {price:.2f}")
            except Exception as e:
                self.write_log(f"[IPO] ❌ {e}")

        def _manage_holding(self, bar):
            if self.pos <= 0:
                return
            self.highest = max(self.highest, bar.close_price)
            self.lowest = min(self.lowest, bar.close_price)
            if bar.close_price >= self.entry_price * (1 + self.profit_take_pct):
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(f"[IPO] 🎯 止盈")
                self._reset_ipo_state()
            elif bar.close_price <= self.entry_price * (1 - self.stop_loss_pct):
                self.sell(bar.close_price - 0.01, abs(self.pos))
                self.write_log(f"[IPO] 🛑 止损")
                self._reset_ipo_state()

        def _reset_ipo_state(self):
            self.today_ipo = False
            self._open_price = 0.0
            self._bars_since_open = 0
            self.highest = 0.0
            self.lowest = 999999.0
            self.entry_time = None

        def on_order(self, order):
            if order.traded > 0 and order.direction == Direction.LONG:
                self.pos = (self.pos or 0) + order.traded
                if self.entry_price == 0:
                    self.entry_price = order.price
                    self.entry_time = datetime.now()
                    self.status = "holding"
            self.put_event()

        def on_trade(self, trade):
            if trade.direction == Direction.LONG:
                self.pos = (self.pos or 0) + trade.volume
                self.entry_price = trade.price
                self.status = "holding"
                self.ipos_tracked += 1
            elif trade.direction == Direction.SHORT:
                self.pos = (self.pos or 0) - trade.volume
                if self.pos <= 0:
                    self._reset_ipo_state()
            self.put_event()
