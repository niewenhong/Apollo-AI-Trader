"""
strategies/base_strategy.py - v2.9.4
策略基类：所有策略的公共父类

v2.9.4 更新：
- 5个 BarGenerator（1M/5M/15M/60M/日）
- on_1m/5m/15m/60m/daily_bar 完整钩子
- regime 感知（get_current_regime / is_regime_tradeable）
- bars_held 超时强平
- close_position() 通用平仓
- 统一移动止盈/硬止损
- _safe_float / _safe_int 防御工具
"""
import time
import logging
from typing import Optional, Dict, Any, Callable, Tuple
from datetime import datetime, time as dtime

from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData, TickData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, Status, Interval
from vnpy.trader.utility import ArrayManager, BarGenerator

try:
    from core.db_manager import DBManager
    HAS_DB = True
except ImportError:
    HAS_DB = False

logger = logging.getLogger("BaseStrategy")


class ApolloBaseStrategy(CtaTemplate):
    """
    所有 Apollo 策略的公共基类。

    分层架构（每个策略按需覆盖）：
    - on_tick(tick)        → 微观执行层（订单流、盘口、即时止损）
    - on_bar(bar)          → 由 BarGenerator 合成后回调（默认转发到 on_1m_bar）
    - on_1m_bar(bar)       → 短期信号层（1分钟，主交易决策）
    - on_5m_bar(bar)       → 中期确认层
    - on_15m_bar(bar)      → 中期确认层
    - on_60m_bar(bar)      → 中观 Regime 更新
    - on_daily_bar(bar)    → 宏观 Regime + 仓位上限

    通用功能：
    - 数据库驱动的 AI 参数加载
    - 统一的交易时段判断（US/HK）
    - 统一的 Kelly 仓位计算
    - 统一的移动止盈/硬止损
    - 统一的交易统计
    - 通知回调接口
    - Regime 感知（从 RegimeEngine 注入）
    """

    parameters = [
        "fixed_size",
        "stop_loss_pct",
        "profit_activation_pct",
        "trailing_stop_pct",
        "max_position_pct",
        "max_holding_bars",
        "session_open_hour",
        "session_open_minute",
        "session_close_hour",
        "session_close_minute",
        "use_regime_filter",
    ]
    variables = [
        "pos", "entry_price", "today_pnl",
        "total_trades", "winning_trades",
        "is_ordering", "trailing_active",
        "highest_since_entry", "trailing_stop",
        "current_regime", "bars_held",
    ]

    DEFAULTS: Dict[str, Any] = {
        "fixed_size": 100,
        "stop_loss_pct": 0.008,
        "profit_activation_pct": 0.02,
        "trailing_stop_pct": 0.005,
        "max_position_pct": 0.08,
        "max_holding_bars": 60,
        "session_open_hour": 9,
        "session_open_minute": 30,
        "session_close_hour": 15,
        "session_close_minute": 50,
        "use_regime_filter": True,
    }

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
        self.trailing_active = False
        self.highest_since_entry = 0.0
        self.trailing_stop = 0.0
        self.current_regime = "unknown"
        self.bars_held = 0

        # 多周期 BarGenerator
        self.bg_1m = BarGenerator(self.on_bar, 1, self.on_1m_bar)
        self.bg_5m = BarGenerator(self.on_bar, 5, self.on_5m_bar)
        self.bg_15m = BarGenerator(self.on_bar, 15, self.on_15m_bar)
        self.bg_60m = BarGenerator(self.on_bar, 60, self.on_60m_bar)
        # 日线：1440 分钟 = 1 天（美股常规 6.5 小时 = 390 分钟，用 390）
        self.bg_daily = BarGenerator(self.on_bar, 390, self.on_daily_bar)

        # ArrayManager（默认 5000 根 1M bar ≈ 8 个交易日，足够所有指标）
        self.am = ArrayManager(5000)

        # 通知回调
        self.notice_callback: Optional[Callable] = None

        # 市场判断
        self.is_us = (".SMART" in vt_symbol) or (".US." in vt_symbol) or vt_symbol.startswith("US.")
        self.is_hk = (".SEHK" in vt_symbol) or (".HK." in vt_symbol) or vt_symbol.startswith("HK.")

        # Regime 引擎引用（外部注入）
        self.regime_engine = None

        # 参数版本
        self._param_version = setting.get("_version", 1)

        # AI 参数加载
        self._load_ai_params()

        self.write_log(f"[INIT] {strategy_name} | {vt_symbol} | US={self.is_us} HK={self.is_hk} v{self._param_version}")

    # ─────────────────────────────
    #  AI 参数加载
    # ─────────────────────────────
    def _load_ai_params(self):
        if not HAS_DB:
            return
        try:
            db = DBManager()
            ai_params = db.get_latest_params(self.vt_symbol, self.__class__.__name__)
            if ai_params:
                for key, value in ai_params.items():
                    if hasattr(self, key):
                        setattr(self, key, value)
                self.write_log(f"AI参数已加载: {list(ai_params.keys())}")
        except Exception as e:
            self.write_log(f"AI参数加载失败: {e}")

    # ─────────────────────────────
    #  生命周期
    # ─────────────────────────────
    def on_init(self):
        self.write_log(f"✅ {self.strategy_name} 初始化完成 (v{self._param_version})")

    def on_start(self):
        self.is_ordering = False
        self.trailing_active = False
        self.bars_held = 0
        self.write_log(f"▶️ {self.strategy_name} 启动")

    def on_stop(self):
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        self.write_log(
            f"⏸ {self.strategy_name} 停止 | "
            f"PnL={self.today_pnl:.2f} 交易={self.total_trades} 胜率={win_rate:.0f}%"
        )

    # ─────────────────────────────
    #  数据入口（统一转发）
    # ─────────────────────────────
    def on_tick(self, tick: TickData):
        """微观层：默认将 tick 喂给所有 BarGenerator"""
        self.bg_1m.update_tick(tick)
        self.bg_5m.update_tick(tick)
        self.bg_15m.update_tick(tick)
        self.bg_60m.update_tick(tick)
        self.bg_daily.update_tick(tick)

    def on_bar(self, bar: BarData):
        """vnpy 引擎回调的 bar 直接转发到各周期 BarGenerator"""
        self.bg_1m.update_bar(bar)
        self.bg_5m.update_bar(bar)
        self.bg_15m.update_bar(bar)
        self.bg_60m.update_bar(bar)
        self.bg_daily.update_bar(bar)

    # ─────────────────────────────
    #  周期钩子（子类按需覆盖）
    # ─────────────────────────────
    def on_1m_bar(self, bar: BarData):
        """短期信号层"""
        self.am.update_bar(bar)
        if self.pos != 0:
            self.bars_held += 1
            # 超时强平
            if self.bars_held >= self.max_holding_bars:
                self.write_log(f"⏰ 持仓超时 {self.bars_held} 根1M bar，强制平仓")
                self.close_position()

    def on_5m_bar(self, bar: BarData):
        """中期确认层（子类覆盖）"""
        pass

    def on_15m_bar(self, bar: BarData):
        """中期确认层（子类覆盖）"""
        pass

    def on_60m_bar(self, bar: BarData):
        """中观 Regime 更新（子类覆盖）"""
        pass

    def on_daily_bar(self, bar: BarData):
        """宏观 Regime + 仓位上限（子类覆盖）"""
        pass

    # ─────────────────────────────
    #  订单/成交处理
    # ─────────────────────────────
    def on_order(self, order: OrderData):
        if order.status in (Status.REJECTED, Status.CANCELLED):
            self.is_ordering = False
            self.write_log(f"📝 订单终态: {order.status.name}")
        elif order.status == Status.ALLTRADED:
            self.is_ordering = False

    def on_trade(self, trade: TradeData):
        self.is_ordering = False
        if trade.direction == Direction.LONG:
            self.entry_price = trade.price
            self.trailing_active = False
            self.highest_since_entry = trade.price
            self.trailing_stop = trade.price * (1 - self.stop_loss_pct)
            self.bars_held = 0
            self.write_log(f"💰 买入: {trade.volume}@{trade.price:.2f}")
        else:
            pnl = (trade.price - self.entry_price) * trade.volume if self.entry_price > 0 else 0
            self.today_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1
            self.write_log(f"💰 卖出: {trade.volume}@{trade.price:.2f} PnL={pnl:.2f}")
        self.put_event()

    # ─────────────────────────────
    #  仓位管理
    # ─────────────────────────────
    def update_trailing_stop(self, price: float) -> bool:
        """返回 True 表示触发止损/止盈"""
        if self.pos == 0 or price <= 0:
            return False

        if price > self.highest_since_entry:
            self.highest_since_entry = price

        # 激活检查
        if not self.trailing_active and self.entry_price > 0:
            pnl_pct = (price - self.entry_price) / self.entry_price
            if pnl_pct >= self.profit_activation_pct:
                self.trailing_active = True
                self.trailing_stop = price * (1 - self.trailing_stop_pct)
                self.write_log(f"🎯 跟踪止损激活 @ {self.trailing_stop:.2f}")

        if self.trailing_active:
            new_stop = price * (1 - self.trailing_stop_pct)
            if new_stop > self.trailing_stop:
                self.trailing_stop = new_stop

        if self.trailing_active and price <= self.trailing_stop:
            return True

        if not self.trailing_active and self.entry_price > 0:
            hard = self.entry_price * (1 - self.stop_loss_pct)
            if price <= hard:
                return True

        return False

    def calc_position_size(self, price: float, win_rate: float = 0.55,
                           win_loss_ratio: float = 1.5,
                           capital: float = 100000.0) -> int:
        """半 Kelly 仓位"""
        p, q = win_rate, 1.0 - win_rate
        b = max(win_loss_ratio, 0.1)
        if price <= 0:
            return self.fixed_size
        f_star = (p * b - q) / b * 0.5
        f_capped = min(max(f_star, 0.01), self.max_position_pct)
        raw = (capital * f_capped) / price
        return max(int(round(raw / self.fixed_size) * self.fixed_size), self.fixed_size)

    def close_position(self):
        """通用平仓：平掉当前所有持仓"""
        if self.pos == 0:
            return
        if self.pos > 0:
            self.sell(self.pos, self.tick.last_price if hasattr(self, 'tick') else 0)
        else:
            self.cover(abs(self.pos), self.tick.last_price if hasattr(self, 'tick') else 0)
        self.write_log(f"🔴 平仓指令发出: pos={self.pos}")

    # ─────────────────────────────
    #  Regime 感知
    # ─────────────────────────────
    def get_current_regime(self) -> str:
        """从 RegimeEngine 获取当前 regime；无引擎时返回 'unknown'"""
        if self.regime_engine is not None:
            try:
                regime = self.regime_engine.get_regime(self.vt_symbol)
                if regime:
                    self.current_regime = regime
                    return regime
            except Exception as e:
                self.write_log(f"Regime查询失败: {e}")
        return self.current_regime

    def is_regime_tradeable(self) -> bool:
        """当前 regime 是否允许开仓"""
        if not self.use_regime_filter:
            return True
        r = self.get_current_regime()
        return r in ("bull_trend", "bear_trend", "strong_bull", "strong_bear")

    # ─────────────────────────────
    #  时间窗口
    # ─────────────────────────────
    def check_time_window(self, bar_dt=None) -> Tuple[bool, bool]:
        """返回 (allow_open, must_close)"""
        if bar_dt is not None:
            now_t = bar_dt.time() if hasattr(bar_dt, 'time') else dtime(bar_dt.hour, bar_dt.minute)
        else:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/New_York") if self.is_us else ZoneInfo("Asia/Hong_Kong")
            now_t = datetime.now(tz).time()

        open_t = dtime(self.session_open_hour, self.session_open_minute)
        close_t = dtime(self.session_close_hour, self.session_close_minute)
        allow_open = open_t <= now_t < close_t
        must_close = now_t >= close_t
        return allow_open, must_close

    # ─────────────────────────────
    #  防御工具
    # ─────────────────────────────
    @staticmethod
    def _safe_float(val, default=0.0) -> float:
        if val is None:
            return default
        if isinstance(val, (int, float)):
            try:
                return float(val)
            except (ValueError, OverflowError):
                return default
        if isinstance(val, str):
            s = val.strip()
            if s == '' or s.upper() == 'N/A':
                return default
            try:
                return float(s)
            except ValueError:
                return default
        return default

    @staticmethod
    def _safe_int(val, default=0) -> int:
        try:
            return int(ApolloBaseStrategy._safe_float(val, default))
        except (ValueError, OverflowError):
            return default

    # ─────────────────────────────
    #  通知 / 统计
    # ─────────────────────────────
    def notify(self, title: str, message: str, level: str = "info"):
        if self.notice_callback:
            try:
                self.notice_callback(self.vt_symbol, 0.0, title, message)
            except Exception:
                pass

    def calc_pnl(self, current_price: float) -> float:
        if self.pos == 0 or self.entry_price == 0:
            return 0.0
        return (current_price - self.entry_price) * self.pos if self.pos > 0 else (self.entry_price - current_price) * abs(self.pos)

    def win_rate(self) -> float:
        return (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
