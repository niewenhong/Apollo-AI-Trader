"""
strategies/futures/momentum_strategy.py - v3.0.1
期货动量策略

v3.0.1 变更：
- 继承 ApolloBaseStrategy（统一 Tick/Bar 管理、AI 参数、Regime 感知）
- 删除独立的 BarGenerator/ArrayManager（由基类提供 bg_1m/am）
- 修复动量计算分母为零风险（加 1e-6 保护）
- 修复 ATR 未初始化时返回 0 导致止损失效
- 修复平仓方向错误（多头应 sell，空头应 cover）
- 修复 on_tick 仍调用 bg.update_tick 的冗余问题
- 新增 need_tick=False（期货策略只需 Bar 数据）
- 新增超时强平（基于基类 bars_held）
- 新增 trailing_stop 支持
- 参数声明规范化（加入 parameters + DEFAULTS）

v3.0.0：
- 显式初始化所有 parameters（防止 setting 缺失导致 AttributeError）
"""
import logging
from typing import Optional

from vnpy.trader.object import BarData, TickData
from vnpy.trader.constant import Direction, Status

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

logger = logging.getLogger("MomentumStrategy")


if _BASE_OK:
    class MomentumStrategy(ApolloBaseStrategy):
        """
        期货动量策略（继承 ApolloBaseStrategy）
        - 自动获得：Tick 管理、多周期 Bar 合成、AI 参数加载、
          Regime 感知、统一平仓/止盈/止损工具
        """
        author = "Apollo"
        version = "v3.0.1"

        # ── 策略参数 ──
        momentum_window = 20
        entry_threshold = 0.02
        fixed_size = 1
        stop_loss_atr = 2.0
        max_holding_bars = 120
        use_trailing = True
        atr_period = 14

        # 合并基类 parameters
        parameters = ApolloBaseStrategy.parameters + [
            "momentum_window", "entry_threshold", "fixed_size",
            "stop_loss_atr", "max_holding_bars", "use_trailing",
            "atr_period",
        ]
        variables = ApolloBaseStrategy.variables + [
            "momentum", "atr_value", "pnl",
        ]

        # ────────────────────────────
        #  初始化
        # ────────────────────────────
        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)

            # 显式初始化（防止 setting 缺失）
            self.momentum_window = int(setting.get("momentum_window", 20))
            self.entry_threshold = float(setting.get("entry_threshold", 0.02))
            self.fixed_size = int(setting.get("fixed_size", 1))
            self.stop_loss_atr = float(setting.get("stop_loss_atr", 2.0))
            self.max_holding_bars = int(setting.get("max_holding_bars", 120))
            self.use_trailing = bool(setting.get("use_trailing", True))
            self.atr_period = int(setting.get("atr_period", 14))

            # 策略状态
            self.momentum = 0.0
            self.atr_value = 0.0
            self.pnl = 0.0
            self._entry_price = 0.0
            self._trailing_stop = 0.0
            self._trailing_active = False

            # 期货策略不需要 Tick
            self.need_tick = False

            self.write_log(
                f"[MOM] ✅ 初始化 | win={self.momentum_window} "
                f"thr={self.entry_threshold:.4f} "
                f"sl_atr={self.stop_loss_atr}"
            )

        # ────────────────────────────
        #  生命周期
        # ────────────────────────────
        def on_init(self):
            try:
                self.load_bar(10, use_database=True)
            except Exception:
                pass
            self.write_log("Momentum策略初始化完成")

        def on_start(self):
            self.write_log("Momentum策略启动")

        def on_stop(self):
            self.write_log(
                f"Momentum策略停止 | pos={self.pos} pnl={self.pnl:.2f}"
            )

        # ────────────────────────────
        #  数据入口
        # ────────────────────────────
        def on_tick(self, tick: TickData):
            """期货策略不需要 Tick"""
            return

        def on_1m_bar(self, bar: BarData):
            """
            1 分钟 Bar 回调（由基类架构驱动）
            此处执行动量计算和交易决策
            """
            # 先调用基类（处理超时强平等通用逻辑）
            super().on_1m_bar(bar)

            # 更新 am（基类 on_1m_bar 已更新，这里确保最新）
            self.am.update_bar(bar)

            if not self.am.inited:
                return

            # ── 计算动量 ──
            n = min(self.momentum_window, len(self.am.close))
            if n < 2:
                return

            close_now = self.am.close[-1]
            close_n = self.am.close[-n]
            denom = close_n + 1e-6  # 防止除零
            self.momentum = (close_now - close_n) / denom

            # ── 计算 ATR（带保护） ──
            try:
                self.atr_value = self.am.atr(self.atr_period, array=False)
            except Exception:
                self.atr_value = 0.0

            if self.atr_value <= 0:
                # ATR 不可用，使用固定百分比兜底
                self.atr_value = abs(close_now) * 0.01

            # ── 持仓管理 / 入场检查 ──
            if self.pos != 0:
                self._manage_position(bar)
            else:
                self._check_entry(bar)

        # ────────────────────────────
        #  入场
        # ────────────────────────────
        def _check_entry(self, bar: BarData):
            """检查入场信号"""
            price = bar.close_price
            size = self.fixed_size

            if self.momentum > self.entry_threshold:
                self.buy(price + 0.01, size)
                self._entry_price = price
                self._trailing_stop = price - self.atr_value * self.stop_loss_atr
                self._trailing_active = False
                self.pnl = 0.0
                self.write_log(
                    f"[MOM] 🚀 多头入场 | momentum={self.momentum:.4f} "
                    f"price={price:.2f}"
                )
            elif self.momentum < -self.entry_threshold:
                self.short(price - 0.01, size)
                self._entry_price = price
                self._trailing_stop = price + self.atr_value * self.stop_loss_atr
                self._trailing_active = False
                self.pnl = 0.0
                self.write_log(
                    f"[MOM] 🔻 空头入场 | momentum={self.momentum:.4f} "
                    f"price={price:.2f}"
                )

        # ────────────────────────────
        #  持仓管理
        # ────────────────────────────
        def _manage_position(self, bar: BarData):
            """管理持仓：trailing stop / 硬止损 / 超时"""
            price = bar.close_price

            if self.pos > 0:
                # ── 多头 ──
                self.pnl = (price - self._entry_price) * self.pos

                # 激活 trailing
                if self.use_trailing and not self._trailing_active:
                    if price >= self._entry_price * 1.01:
                        self._trailing_active = True
                        self._trailing_stop = price - self.atr_value * self.stop_loss_atr
                        self.write_log(
                            f"[MOM] 🎯 Trailing 激活 @ {self._trailing_stop:.2f}"
                        )

                # 更新 trailing stop
                if self._trailing_active:
                    new_stop = price - self.atr_value * self.stop_loss_atr
                    if new_stop > self._trailing_stop:
                        self._trailing_stop = new_stop

                # 止损判断
                if self._trailing_active:
                    stop = self._trailing_stop
                else:
                    stop = self._entry_price - self.atr_value * self.stop_loss_atr

                if price <= stop:
                    self.sell(price - 0.01, abs(self.pos))
                    self.write_log(
                        f"[MOM] 🛑 多头出场 | price={price:.2f} "
                        f"stop={stop:.2f} pnl={self.pnl:.2f}"
                    )
                    self._reset_state()

            elif self.pos < 0:
                # ── 空头 ──
                self.pnl = (self._entry_price - price) * abs(self.pos)

                # 激活 trailing
                if self.use_trailing and not self._trailing_active:
                    if price <= self._entry_price * 0.99:
                        self._trailing_active = True
                        self._trailing_stop = price + self.atr_value * self.stop_loss_atr
                        self.write_log(
                            f"[MOM] 🎯 Trailing 激活 @ {self._trailing_stop:.2f}"
                        )

                # 更新 trailing stop
                if self._trailing_active:
                    new_stop = price + self.atr_value * self.stop_loss_atr
                    if new_stop < self._trailing_stop or self._trailing_stop == 0:
                        self._trailing_stop = new_stop

                # 止损判断
                if self._trailing_active:
                    stop = self._trailing_stop
                else:
                    stop = self._entry_price + self.atr_value * self.stop_loss_atr

                if price >= stop:
                    self.cover(price + 0.01, abs(self.pos))
                    self.write_log(
                        f"[MOM] 🛑 空头出场 | price={price:.2f} "
                        f"stop={stop:.2f} pnl={self.pnl:.2f}"
                    )
                    self._reset_state()

            # 超时强平（基类 bars_held 计数）
            if self.bars_held >= self.max_holding_bars:
                if self.pos > 0:
                    self.sell(price - 0.01, abs(self.pos))
                elif self.pos < 0:
                    self.cover(price + 0.01, abs(self.pos))
                self.write_log(f"[MOM] ⏰ 超时平仓 {self.bars_held}分钟")
                self._reset_state()

        # ────────────────────────────
        #  状态重置
        # ────────────────────────────
        def _reset_state(self):
            """重置交易状态"""
            self._entry_price = 0.0
            self._trailing_stop = 0.0
            self._trailing_active = False
            self.pnl = 0.0

        # ────────────────────────────
        #  订单/成交处理
        # ────────────────────────────
        def on_order(self, order: OrderData):
            """订单回报"""
            if order.status in (Status.REJECTED, Status.CANCELLED):
                self.write_log(f"[MOM] 📝 订单终态: {order.status.name}")
            elif order.status == Status.ALLTRADED:
                self.write_log(f"[MOM] ✅ 全部成交 @ {order.price:.2f}")
            # 调用基类处理
            super().on_order(order)

        def on_trade(self, trade: TradeData):
            """成交回报"""
            self.write_log(
                f"[MOM] 💰 {trade.direction} "
                f"{trade.volume}@{trade.price:.2f}"
            )
            if trade.direction == Direction.LONG:
                self._entry_price = trade.price
                self._trailing_stop = trade.price - self.atr_value * self.stop_loss_atr
                self._trailing_active = False
                self.pnl = 0.0
            elif trade.direction == Direction.SHORT:
                self._entry_price = trade.price
                self._trailing_stop = trade.price + self.atr_value * self.stop_loss_atr
                self._trailing_active = False
                self.pnl = 0.0
            # 调用基类处理
            super().on_trade(trade)

else:
    # ────────────────────────────
    #  Fallback：基类不可用时独立运行
    # ────────────────────────────
    from vnpy_ctastrategy import CtaTemplate
    from vnpy.trader.utility import BarGenerator, ArrayManager

    class MomentumStrategy(CtaTemplate):
        author = "Apollo"
        version = "v3.0.1-fallback"

        momentum_window = 20
        entry_threshold = 0.02
        fixed_size = 1
        stop_loss_atr = 2.0
        max_holding_bars = 120
        use_trailing = True
        atr_period = 14

        parameters = [
            "momentum_window", "entry_threshold", "fixed_size",
            "stop_loss_atr", "max_holding_bars", "use_trailing",
            "atr_period",
        ]
        variables = ["pos", "momentum", "atr_value", "pnl"]

        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)

            self.momentum_window = int(setting.get("momentum_window", 20))
            self.entry_threshold = float(setting.get("entry_threshold", 0.02))
            self.fixed_size = int(setting.get("fixed_size", 1))
            self.stop_loss_atr = float(setting.get("stop_loss_atr", 2.0))
            self.max_holding_bars = int(setting.get("max_holding_bars", 120))
            self.use_trailing = bool(setting.get("use_trailing", True))
            self.atr_period = int(setting.get("atr_period", 14))

            self.momentum = 0.0
            self.atr_value = 0.0
            self.pnl = 0.0
            self._entry_price = 0.0
            self._trailing_stop = 0.0
            self._trailing_active = False
            self.need_tick = False
            self.pos = 0
            self.bars_held = 0

            self.bg = BarGenerator(self.on_bar)
            self.am = ArrayManager(size=100)

            self.write_log(f"[MOM] ✅ Fallback 初始化 | {vt_symbol}")

        def on_init(self):
            try:
                self.load_bar(10, use_database=True)
            except Exception:
                pass
            self.write_log("Momentum策略初始化完成(fallback)")

        def on_start(self):
            self.write_log("Momentum策略启动")

        def on_stop(self):
            self.write_log(f"Momentum停止 | pos={self.pos}")

        def on_tick(self, tick):
            return

        def on_bar(self, bar):
            self.am.update_bar(bar)
            if not self.am.inited:
                return

            n = min(self.momentum_window, len(self.am.close))
            if n < 2:
                return

            close_now = self.am.close[-1]
            close_n = self.am.close[-n]
            self.momentum = (close_now - close_n) / (close_n + 1e-6)

            try:
                self.atr_value = self.am.atr(self.atr_period, array=False)
            except Exception:
                self.atr_value = abs(close_now) * 0.01

            if self.atr_value <= 0:
                self.atr_value = abs(close_now) * 0.01

            if self.pos != 0:
                self._manage_position(bar)
            else:
                self._check_entry(bar)

        def _check_entry(self, bar):
            price = bar.close_price
            if self.momentum > self.entry_threshold:
                self.buy(price + 0.01, self.fixed_size)
                self._entry_price = price
                self._trailing_stop = price - self.atr_value * self.stop_loss_atr
                self.write_log(f"[MOM] 🚀 多头 @ {price:.2f}")
            elif self.momentum < -self.entry_threshold:
                self.short(price - 0.01, self.fixed_size)
                self._entry_price = price
                self._trailing_stop = price + self.atr_value * self.stop_loss_atr
                self.write_log(f"[MOM] 🔻 空头 @ {price:.2f}")

        def _manage_position(self, bar):
            price = bar.close_price
            if self.pos > 0:
                self.pnl = (price - self._entry_price) * self.pos
                if self.use_trailing and not self._trailing_active:
                    if price >= self._entry_price * 1.01:
                        self._trailing_active = True
                        self._trailing_stop = price - self.atr_value * self.stop_loss_atr
                if self._trailing_active:
                    new_stop = price - self.atr_value * self.stop_loss_atr
                    if new_stop > self._trailing_stop:
                        self._trailing_stop = new_stop
                stop = self._trailing_stop if self._trailing_active else (
                    self._entry_price - self.atr_value * self.stop_loss_atr
                )
                if price <= stop:
                    self.sell(price - 0.01, abs(self.pos))
                    self.write_log(f"[MOM] 🛑 多头出场 pnl={self.pnl:.2f}")
                    self._reset_state()
            elif self.pos < 0:
                self.pnl = (self._entry_price - price) * abs(self.pos)
                if self.use_trailing and not self._trailing_active:
                    if price <= self._entry_price * 0.99:
                        self._trailing_active = True
                        self._trailing_stop = price + self.atr_value * self.stop_loss_atr
                if self._trailing_active:
                    new_stop = price + self.atr_value * self.stop_loss_atr
                    if new_stop < self._trailing_stop or self._trailing_stop == 0:
                        self._trailing_stop = new_stop
                stop = self._trailing_stop if self._trailing_active else (
                    self._entry_price + self.atr_value * self.stop_loss_atr
                )
                if price >= stop:
                    self.cover(price + 0.01, abs(self.pos))
                    self.write_log(f"[MOM] 🛑 空头出场 pnl={self.pnl:.2f}")
                    self._reset_state()

        def _reset_state(self):
            self._entry_price = 0.0
            self._trailing_stop = 0.0
            self._trailing_active = False
            self.pnl = 0.0

        def on_order(self, order):
            self.write_log(f"[MOM] 📝 {order.status.name}")

        def on_trade(self, trade):
            self.write_log(f"[MOM] 💰 {trade.direction} {trade.volume}@{trade.price:.2f}")
            self.put_event()
