"""
strategies/equity/managed_position_strategy.py - v3.8.0
外部持仓接管策略

功能：
- 自动接管非本系统买入的持仓
- 仅做风险管理（止损/止盈/跟踪止盈）
- 不主动开仓
- 持仓盈亏不计入策略绩效评分
- 持仓清空后自动退役

使用场景：
- 用户在富途APP手动买入股票
- 系统启动后检测到未管理持仓
- LifecycleManager 自动创建此策略接管
"""
import logging
from datetime import datetime
from vnpy.trader.object import BarData, TradeData, OrderData
from vnpy.trader.constant import Status, Direction

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("ManagedPosition")


class ManagedPositionStrategy(BaseStrategy):
    """
    外部持仓接管策略

    特性：
    - 启动时自动设置 pos 和 entry_price（从参数读取）
    - 仅监控止损止盈，不开新仓
    - 跟踪止盈自动激活
    - 持仓清空后通知 LifecycleManager 移除
    """

    author = "Apollo Team"

    # 接管策略参数
    stop_loss_pct = 0.05        # 5% 硬止损
    trailing_stop_pct = 0.03    # 3% 跟踪止盈
    profit_activation_pct = 0.02 # 2% 激活跟踪
    max_holding_bars = 480       # 最多持有 480 根 1M bar（约 8 小时）
    check_interval_seconds = 60  # 每分钟检查一次

    # 标记
    is_adopt = True              # 标记为接管策略

    parameters = BaseStrategy.parameters + [
        "stop_loss_pct",
        "trailing_stop_pct",
        "profit_activation_pct",
        "max_holding_bars",
        "is_adopt",
    ]
    variables = BaseStrategy.variables + [
        "adopted_quantity",
        "adopted_cost",
        "high_water_mark",
    ]

    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 接管专用状态
        self.adopted_quantity = setting.get('fixed_size', 0)
        self.adopted_cost = setting.get('entry_price', 0.0)
        self.high_water_mark = 0.0
        self._trailing_active = False
        self._bars_counted = 0
        self._liquidated = False

    # ────────────────────────────
    #  生命周期
    # ────────────────────────────
    def on_init(self):
        """初始化"""
        super().on_init()
        self.write_log(
            f"🔄 接管策略初始化: qty={self.adopted_quantity} "
            f"cost={self.adopted_cost:.2f}"
        )

    def on_start(self):
        """启动——接管持仓"""
        super().on_start()

        # 设置初始持仓（如果 pos 还未被设置）
        if self.pos == 0 and self.adopted_quantity > 0:
            # 模拟买入成交来设置持仓
            self.pos = self.adopted_quantity
            self.entry_price = self.adopted_cost
            self.high_water_mark = self.adopted_cost
            self.write_log(
                f"✅ 接管持仓: {self.pos}股 @ {self.entry_price:.2f}"
            )

        self._trailing_active = False
        self._bars_counted = 0
        self._liquidated = False

    def on_stop(self):
        """停止"""
        self.write_log(
            f"⏸ 接管策略停止: pos={self.pos} "
            f"entry={self.entry_price:.2f}"
        )
        super().on_stop()

    # ────────────────────────────
    #  K线回调——仅做风控管理
    # ────────────────────────────
    def on_1m_bar(self, bar: BarData):
        """每分钟检查止损止盈"""
        super().on_1m_bar(bar)  # 调用基类超时检查

        if self.pos == 0 or self._liquidated:
            return

        self._bars_counted += 1
        price = bar.close_price

        # 更新最高点
        if price > self.high_water_mark:
            self.high_water_mark = price

        # 检查止损/止盈
        should_exit = self._check_exit(price)

        if should_exit:
            self._execute_exit(price)
            return

        # 超时强平（额外保护）
        if self._bars_counted >= self.max_holding_bars:
            self.write_log(
                f"⏰ 接管持仓超时 "
                f"({self._bars_counted}>{self.max_holding_bars})，强平"
            )
            self._execute_exit(price)

    def on_5m_bar(self, bar: BarData):
        """5分钟K线——记录状态"""
        if self.pos == 0:
            return
        pnl_pct = (bar.close_price - self.entry_price) / self.entry_price * 100
        self.write_log(
            f"[ADOPT] {bar.datetime.strftime('%H:%M')} "
            f"price={bar.close_price:.2f} "
            f"PnL={pnl_pct:+.2f}% "
            f"trailing={self._trailing_active}"
        )

    # ────────────────────────────
    #  成交处理
    # ────────────────────────────
    def on_trade(self, trade: TradeData):
        """接管策略的成交处理"""
        if trade.direction == Direction.SHORT and self.pos < 0:
            # 卖出平仓成交
            pnl = (self.entry_price - trade.price) * trade.volume
            self.write_log(
                f"💰 接管平仓: {trade.volume}@{trade.price:.2f} "
                f"PnL={pnl:+.2f}"
            )
            self.pos += trade.volume  # pos 是负的，加回
            if self.pos == 0:
                self._liquidated = True
                self.write_log("🏁 接管持仓已清空，策略可安全移除")

        elif trade.direction == Direction.LONG and self.pos > 0:
            # 买入平仓（做空的情况）
            pnl = (trade.price - self.entry_price) * trade.volume
            self.write_log(
                f"💰 接管平仓: {trade.volume}@{trade.price:.2f} "
                f"PnL={pnl:+.2f}"
            )
            self.pos -= trade.volume
            if self.pos == 0:
                self._liquidated = True

    def on_order(self, order: OrderData):
        """订单状态更新"""
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self.write_log(f"⚠️ 接管订单异常: {order.status.name}")
        elif order.status == Status.ALLTRADED:
            self.write_log(f"✅ 接管订单完成")

    # ────────────────────────────
    #  内部方法
    # ────────────────────────────
    def _check_exit(self, price: float) -> bool:
        """检查是否触发退出条件"""
        if self.entry_price <= 0 or price <= 0:
            return False

        pnl_pct = (price - self.entry_price) / self.entry_price

        # 1. 硬止损
        if pnl_pct <= -self.stop_loss_pct:
            self.write_log(f"🛑 触发硬止损: {pnl_pct*100:.2f}%")
            return True

        # 2. 跟踪止盈激活检查
        if not self._trailing_active:
            if pnl_pct >= self.profit_activation_pct:
                self._trailing_active = True
                self.write_log(f"🎯 跟踪止盈激活 @ {price:.2f}")
                return False

        # 3. 跟踪止盈触发
        if self._trailing_active:
            trail_stop = self.high_water_mark * (1 - self.trailing_stop_pct)
            if price <= trail_stop:
                self.write_log(
                    f"🎯 触发跟踪止盈: price={price:.2f} "
                    f"stop={trail_stop:.2f} high={self.high_water_mark:.2f}"
                )
                return True

        return False

    def _execute_exit(self, price: float):
        """执行平仓"""
        if self.pos > 0:
            self.sell(price, abs(self.pos))
            self.write_log(f"🔴 接管卖出: {abs(self.pos)}@{price:.2f}")
        elif self.pos < 0:
            self.cover(price, abs(self.pos))
            self.write_log(f"🔴 接管回补: {abs(self.pos)}@{price:.2f}")

    # ────────────────────────────
    #  禁止开仓（接管策略不主动交易）
    # ────────────────────────────
    def buy(self, price, volume, stop=False, lock=False, net=False):
        self.write_log(f"⚠️ 接管策略禁止开仓: buy {volume}@{price}")
        return []

    def short(self, price, volume, stop=False, lock=False, net=False):
        self.write_log(f"⚠️ 接管策略禁止开仓: short {volume}@{price}")
        return []
