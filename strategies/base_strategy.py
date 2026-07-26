"""
strategies/base_strategy.py - v2.8.0
策略基类：所有策略的公共父类，提供通用功能
继承 vnpy CtaTemplate，减少各策略的重复代码
"""
import time
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import datetime

from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData, TickData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, Status

try:
    from core.db_manager import CustomDBManager
    HAS_DB = True
except ImportError:
    HAS_DB = False

logger = logging.getLogger("BaseStrategy")


class ApolloBaseStrategy(CtaTemplate):
    """
    所有 Apollo 策略的公共基类
    
    提供的通用功能：
    - 数据库驱动的 AI 参数加载
    - 统一的交易时段判断（US/HK）
    - 统一的 Kelly 仓位计算
    - 统一的移动止盈/止损管理
    - 统一的交易统计（胜率、盈亏）
    - 统一的 on_trade/on_order 处理
    - 通知回调接口
    """

    # 子类需要覆盖的参数
    parameters = [
        "fixed_size",
        "stop_loss_pct",
        "profit_activation_pct",
        "trailing_stop_pct",
        "max_position_pct",
    ]
    variables = [
        "pos", "entry_price", "today_pnl",
        "total_trades", "winning_trades",
        "is_ordering",
    ]

    # ──────────────────────────────
    #  默认参数
    # ──────────────────────────────
    DEFAULTS: Dict[str, Any] = {
        "fixed_size": 100,
        "stop_loss_pct": 0.008,
        "profit_activation_pct": 0.02,
        "trailing_stop_pct": 0.005,
        "max_position_pct": 0.08,
    }

    # ──────────────────────────────
    #  初始化
    # ──────────────────────────────
    def __init__(self, cta_engine, strategy_name: str, vt_symbol: str, setting: dict):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 合并默认参数
        merged = {**self.DEFAULTS, **setting}
        for key, value in merged.items():
            if hasattr(self, key) or key in self.parameters:
                setattr(self, key, value)

        # 通用状态
        self.entry_price = 0.0
        self.today_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.is_ordering = False

        # 移动止盈
        self._trailing_active = False
        self._trailing_stop = 0.0
        self._highest_since_entry = 0.0

        # 通知回调
        self.notice_callback = None

        # 市场判断
        self.is_us = (".SMART" in vt_symbol) or (".US" in vt_symbol)

        # 参数版本
        self._param_version = setting.get("_version", 1)

        # AI 参数加载
        self._load_ai_params()

    # ──────────────────────────────
    #  AI 参数加载
    # ──────────────────────────────
    def _load_ai_params(self):
        if not HAS_DB:
            return
        try:
            db = CustomDBManager()
            ai_params = db.get_latest_params(self.vt_symbol, self.__class__.__name__)
            if ai_params:
                for key, value in ai_params.items():
                    if hasattr(self, key):
                        setattr(self, key, value)
                self.write_log(f"AI参数已加载: {list(ai_params.keys())}")
        except Exception as e:
            self.write_log(f"AI参数加载失败: {e}")

    # ──────────────────────────────
    #  生命周期（子类可覆盖）
    # ──────────────────────────────
    def on_init(self):
        self.write_log(f"✅ {self.strategy_name} 初始化完成 (v{self._param_version})")

    def on_start(self):
        self.is_ordering = False
        self._trailing_active = False
        self.write_log(f"▶️ {self.strategy_name} 启动")

    def on_stop(self):
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        self.write_log(
            f"⏸ {self.strategy_name} 停止 | "
            f"PnL={self.today_pnl:.2f} 交易={self.total_trades} 胜率={win_rate:.0f}%"
        )

    # ──────────────────────────────
    #  通用订单/成交处理
    # ──────────────────────────────
    def on_order(self, order: OrderData):
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self.is_ordering = False
            self.write_log(f"📝 订单终态: {order.status.name}")

    def on_trade(self, trade: TradeData):
        self.is_ordering = False
        direction = "买入" if trade.direction == Direction.LONG else "卖出"

        if trade.direction == Direction.LONG:
            self.entry_price = trade.price
            self._trailing_active = False
            self._highest_since_entry = trade.price
            self._trailing_stop = trade.price * (1 - self.stop_loss_pct)
            self.write_log(f"💰 买入: {trade.volume}@{trade.price:.2f}")
        else:
            pnl = (trade.price - self.entry_price) * trade.volume if self.entry_price > 0 else 0
            self.today_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1
            self.write_log(f"💰 卖出: {trade.volume}@{trade.price:.2f} PnL={pnl:.2f}")

        self.put_event()

    # ──────────────────────────────
    #  通用仓位管理
    # ──────────────────────────────
    def update_trailing_stop(self, price: float) -> bool:
        """
        更新跟踪止损，返回是否触发止损
        子类在持仓时每tick/bar调用
        """
        if self.pos == 0:
            return False

        # 更新最高价
        if price > self._highest_since_entry:
            self._highest_since_entry = price

        # 激活检查
        if not self._trailing_active and self.entry_price > 0:
            pnl_pct = (price - self.entry_price) / self.entry_price
            if pnl_pct >= self.profit_activation_pct:
                self._trailing_active = True
                self._trailing_stop = price * (1 - self.trailing_stop_pct)
                self.write_log(f"🎯 跟踪止损激活 @ {self._trailing_stop:.2f}")

        # 上移止损线
        if self._trailing_active:
            new_stop = price * (1 - self.trailing_stop_pct)
            if new_stop > self._trailing_stop:
                self._trailing_stop = new_stop

        # 检查触发
        if self._trailing_active and price <= self._trailing_stop:
            return True

        # ATR 硬止损（未激活跟踪时）
        if not self._trailing_active and self.entry_price > 0:
            hard_stop = self.entry_price * (1 - self.stop_loss_pct)
            if price <= hard_stop:
                return True

        return False

    def calc_position_size(self, price: float, win_rate: float = 0.55,
                           win_loss_ratio: float = 1.5,
                           capital: float = 100000.0) -> int:
        """
        Kelly 公式仓位计算
        f* = (p*b - q) / b，半 Kelly 更保守
        """
        p = win_rate
        q = 1.0 - p
        b = max(win_loss_ratio, 0.1)

        if price <= 0:
            return self.fixed_size

        f_star = (p * b - q) / b * 0.5  # 半 Kelly
        f_capped = min(max(f_star, 0.01), self.max_position_pct)

        raw = (capital * f_capped) / price
        return max(int(round(raw / self.fixed_size) * self.fixed_size), self.fixed_size)

    # ──────────────────────────────
    #  通用工具
    # ──────────────────────────────
    def notify(self, title: str, message: str, level: str = "info"):
        """发送通知（通过回调或 Telegram）"""
        if self.notice_callback:
            try:
                self.notice_callback(self.vt_symbol, 0.0, title, message)
            except Exception:
                pass

    def calc_pnl(self, current_price: float) -> float:
        """计算当前浮动盈亏"""
        if self.pos == 0 or self.entry_price == 0:
            return 0.0
        if self.pos > 0:
            return (current_price - self.entry_price) * self.pos
        else:
            return (self.entry_price - current_price) * abs(self.pos)

    def win_rate(self) -> float:
        """计算胜率"""
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100
