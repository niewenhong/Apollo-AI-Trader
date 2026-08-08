"""
strategies/base_strategy.py - v3.8.0
策略基类：所有策略的公共父类

v3.8.0 变更：
- 集成 LifecycleManager 下单前检查
- buy/sell/short/cover 增加下单日志
- 支持多用户（user_id 注入）
- 接管策略标记（is_adopt）
- 与 AccountManager 协作获取用户资金
"""
import time
import logging
from typing import Optional, Dict, Any, Callable, Tuple

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


class BaseStrategy(CtaTemplate):
    """
    所有 Apollo 策略的公共基类 (v3.8.0)

    分层架构：
    - on_tick(tick)        → 微观执行层
    - on_bar(bar)          → 转发到 BarGenerator
    - on_1m_bar(bar)       → 短期信号层
    - on_5m_bar(bar)       → 中期确认层
    - on_15m_bar(bar)      → 中期确认层
    - on_60m_bar(bar)      → 中观 Regime
    - on_daily_bar(bar)     → 宏观 Regime + 仓位上限

    交易保护：
    - on_init:  _trading_allowed = False
    - on_start: _trading_allowed = True
    - buy/sell/short/cover 自动检查

    v3.8.0 新增：
    - lifecycle_manager: 策略生命周期总管（外部注入）
    - user_id: 当前用户ID（外部注入）
    - is_adopt: 是否为接管策略
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
        "user_id",
        "is_adopt",
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
        "use_regime_filter": False,
        "user_id": "SYSTEM",
        "is_adopt": False,
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
        self.bg_daily = BarGenerator(self.on_bar, 390, self.on_daily_bar)

        # ArrayManager
        self.am = ArrayManager(5000)

        # 通知回调
        self.notice_callback: Optional[Callable] = None

        # 市场判断
        self.is_us = (".SMART" in vt_symbol) or (".US." in vt_symbol) or vt_symbol.startswith("US.")
        self.is_hk = (".SEHK" in vt_symbol) or (".HK." in vt_symbol) or vt_symbol.startswith("HK.")

        # 参数版本
        self._param_version = setting.get("_version", 1)

        # OrderManager（外部注入）
        self.order_manager = None
        self.use_smart_routing = True

        # LifecycleManager（外部注入 - v3.8.0）
        self.lifecycle_manager = None

        # ---- 交易许可标志 ----
        self._trading_allowed = False

        # AI 参数加载
        self._load_ai_params()

        # Regime
        self.current_regime = setting.get("regime", self.current_regime)

        self.write_log(
            f"[INIT] {strategy_name} | {vt_symbol} | "
            f"US={self.is_us} HK={self.is_hk} "
            f"user={getattr(self, 'user_id', 'SYSTEM')} "
            f"v{self._param_version}"
        )

    # ────────────────────────────
    #  AI 参数加载
    # ────────────────────────────
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

    # ────────────────────────────
    #  生命周期
    # ────────────────────────────
    def on_init(self):
        self._trading_allowed = False
        self.write_log(f"✅ {self.strategy_name} 初始化完成 (v{self._param_version}) | 交易已锁定")

    def on_start(self):
        self._trading_allowed = True
        self.is_ordering = False
        self.trailing_active = False
        self.bars_held = 0
        self.write_log(f"▶ {self.strategy_name} 启动 | 交易已开放")

    def on_stop(self):
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        self.write_log(
            f"⏸ {self.strategy_name} 停止 | "
            f"PnL={self.today_pnl:.2f} 交易={self.total_trades} 胜率={win_rate:.0f}%"
        )

    def is_trading_allowed(self) -> bool:
        return self._trading_allowed

    # ────────────────────────────
    #  数据入口
    # ────────────────────────────
    def on_tick(self, tick: TickData):
        self.bg_1m.update_tick(tick)
        self.bg_5m.update_tick(tick)
        self.bg_15m.update_tick(tick)
        self.bg_60m.update_tick(tick)
        self.bg_daily.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg_1m.update_bar(bar)
        self.bg_5m.update_bar(bar)
        self.bg_15m.update_bar(bar)
        self.bg_60m.update_bar(bar)
        self.bg_daily.update_bar(bar)

    # ────────────────────────────
    #  周期钩子
    # ────────────────────────────
    def on_1m_bar(self, bar: BarData):
        self.am.update_bar(bar)
        if self.pos != 0:
            self.bars_held += 1
            if self.bars_held >= self.max_holding_bars:
                self.write_log(f"⏰ 持仓超时 {self.bars_held} 根1M bar，强制平仓")
                self.close_position()

    def on_5m_bar(self, bar: BarData):
        pass

    def on_15m_bar(self, bar: BarData):
        pass

    def on_60m_bar(self, bar: BarData):
        pass

    def on_daily_bar(self, bar: BarData):
        pass

    # ────────────────────────────
    #  订单/成交处理
    # ────────────────────────────
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
            self.write_log(f"💰 卖出: {trade.volume}@{trade.price:.2f} PnL={pnl:+.2f}")

        # 通知 LifecycleManager
        if self.lifecycle_manager:
            self.lifecycle_manager.on_trade(self.strategy_name, {
                'pnl': pnl if trade.direction != Direction.LONG else 0,
                'price': trade.price,
                'volume': trade.volume,
                'direction': 'LONG' if trade.direction == Direction.LONG else 'SHORT',
            })

        self.put_event()

    # ────────────────────────────
    #  交易保护 + 智能路由 + 生命周期检查
    # ────────────────────────────
    def _check_trading_allowed(self) -> bool:
        if not self._trading_allowed:
            self.write_log("⚠️ 交易未开放，忽略下单请求（回放阶段）")
            return False
        return True

    def buy(self, price, volume, stop=False, lock=False, net=False):
        # 1. 基本交易许可
        if not self._check_trading_allowed():
            return []
        # 2. 生命周期检查（v3.8.0）
        if self.lifecycle_manager:
            user_id = getattr(self, 'user_id', 'SYSTEM')
            allowed, reason = self.lifecycle_manager.can_send_order(
                self.strategy_name, 'LONG', price, volume,
                self.vt_symbol, user_id
            )
            if not allowed:
                self.write_log(f"[Lifecycle] 🚫 {reason}")
                return []
        # 3. 智能路由 or 直接下单
        if self._should_route():
            symbol = self.vt_symbol.split('.')[0]
            self.order_manager.submit_signal(
                symbol=symbol, direction='LONG', price=float(price),
                volume=int(volume), offset='OPEN',
                strategy_name=self.strategy_name, auto_size=(int(volume) <= 0)
            )
            result = [f"SMART_{self.strategy_name}_{symbol}"]
        else:
            result = super().buy(price, volume, stop=stop, lock=lock, net=net)
        # 4. 日志 + 通知
        self.write_log(f"[BUY] price={price}, volume={volume}, result={result}")
        if self.lifecycle_manager and result:
            self.lifecycle_manager.on_order_sent(self.strategy_name, result)
        return result

    def sell(self, price, volume, stop=False, lock=False, net=False):
        if not self._check_trading_allowed():
            return []
        if self.lifecycle_manager:
            user_id = getattr(self, 'user_id', 'SYSTEM')
            allowed, reason = self.lifecycle_manager.can_send_order(
                self.strategy_name, 'SHORT', price, volume,
                self.vt_symbol, user_id
            )
            if not allowed:
                self.write_log(f"[Lifecycle] 🚫 {reason}")
                return []
        if self._should_route():
            symbol = self.vt_symbol.split('.')[0]
            self.order_manager.submit_signal(
                symbol=symbol, direction='SHORT', price=float(price),
                volume=int(volume), offset='CLOSE',
                strategy_name=self.strategy_name, auto_size=False
            )
            result = [f"SMART_{self.strategy_name}_{symbol}"]
        else:
            result = super().sell(price, volume, stop=stop, lock=lock, net=net)
        self.write_log(f"[SELL] price={price}, volume={volume}, result={result}")
        if self.lifecycle_manager and result:
            self.lifecycle_manager.on_order_sent(self.strategy_name, result)
        return result

    def short(self, price, volume, stop=False, lock=False, net=False):
        if not self._check_trading_allowed():
            return []
        if self.lifecycle_manager:
            user_id = getattr(self, 'user_id', 'SYSTEM')
            allowed, reason = self.lifecycle_manager.can_send_order(
                self.strategy_name, 'SHORT', price, volume,
                self.vt_symbol, user_id
            )
            if not allowed:
                self.write_log(f"[Lifecycle] 🚫 {reason}")
                return []
        if self._should_route():
            symbol = self.vt_symbol.split('.')[0]
            self.order_manager.submit_signal(
                symbol=symbol, direction='SHORT', price=float(price),
                volume=int(volume), offset='OPEN',
                strategy_name=self.strategy_name, auto_size=(int(volume) <= 0)
            )
            result = [f"SMART_{self.strategy_name}_{symbol}"]
        else:
            result = super().short(price, volume, stop=stop, lock=lock, net=net)
        self.write_log(f"[SHORT] price={price}, volume={volume}, result={result}")
        if self.lifecycle_manager and result:
            self.lifecycle_manager.on_order_sent(self.strategy_name, result)
        return result

    def cover(self, price, volume, stop=False, lock=False, net=False):
        if not self._check_trading_allowed():
            return []
        if self.lifecycle_manager:
            user_id = getattr(self, 'user_id', 'SYSTEM')
            allowed, reason = self.lifecycle_manager.can_send_order(
                self.strategy_name, 'LONG', price, volume,
                self.vt_symbol, user_id
            )
            if not allowed:
                self.write_log(f"[Lifecycle] 🚫 {reason}")
                return []
        if self._should_route():
            symbol = self.vt_symbol.split('.')[0]
            self.order_manager.submit_signal(
                symbol=symbol, direction='LONG', price=float(price),
                volume=int(volume), offset='CLOSE',
                strategy_name=self.strategy_name, auto_size=False
            )
            result = [f"SMART_{self.strategy_name}_{symbol}"]
        else:
            result = super().cover(price, volume, stop=stop, lock=lock, net=net)
        self.write_log(f"[COVER] price={price}, volume={volume}, result={result}")
        if self.lifecycle_manager and result:
            self.lifecycle_manager.on_order_sent(self.strategy_name, result)
        return result

    def _should_route(self):
        return (self.use_smart_routing
                and self.order_manager is not None
                and hasattr(self.order_manager, 'submit_signal'))

    # ────────────────────────────
    #  仓位管理
    # ────────────────────────────
    def update_trailing_stop(self, price: float) -> bool:
        if self.pos == 0 or price <= 0:
            return False
        if price > self.highest_since_entry:
            self.highest_since_entry = price
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
                           capital: float = None) -> int:
        """半 Kelly 仓位，按用户可用资金 + 策略层级调整"""
        # 获取用户可用资金
        if capital is None:
            user_id = getattr(self, 'user_id', 'SYSTEM')
            if self.lifecycle_manager and self.lifecycle_manager.account:
                capital = self.lifecycle_manager.account.get_available_capital(user_id)
            else:
                capital = 100000.0  # 默认值

        # 按层级缩放
        if self.lifecycle_manager:
            info = self.lifecycle_manager._get_strategy_info(self.strategy_name)
            if info:
                tier = info.get('tier', 'TRIAL')
                from core.strategy_lifecycle_manager import StrategyTier, LifecycleAction
                try:
                    tier_enum = StrategyTier(tier)
                    ratio = LifecycleAction  # placeholder
                except:
                    pass

        p, q = win_rate, 1.0 - win_rate
        b = max(win_loss_ratio, 0.1)
        if price <= 0:
            return self.fixed_size
        f_star = (p * b - q) / b * 0.5
        f_capped = min(max(f_star, 0.01), self.max_position_pct)
        raw = (capital * f_capped) / price
        return max(int(round(raw / self.fixed_size) * self.fixed_size), self.fixed_size)

    def close_position(self):
        if self.pos == 0:
            return
        last_price = getattr(self, 'last_price', 0.0)
        if self.pos > 0:
            self.sell(last_price if last_price > 0 else self.entry_price, abs(self.pos))
        else:
            self.cover(last_price if last_price > 0 else self.entry_price, abs(self.pos))
        self.write_log(f"🔴 平仓指令发出: pos={self.pos}")

    # ────────────────────────────
    #  Regime
    # ────────────────────────────
    def get_current_regime(self) -> str:
        return self.current_regime

    # ────────────────────────────
    #  时间窗口
    # ────────────────────────────
    def check_time_window(self, bar_dt=None) -> Tuple[bool, bool]:
        from datetime import time as dtime
        from datetime import datetime
        if bar_dt is not None:
            now_t = bar_dt.time() if hasattr(bar_dt, 'time') else dtime(bar_dt.hour, bar_dt.minute)
        else:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo("America/New_York") if self.is_us else ZoneInfo("Asia/Hong_Kong")
            except ImportError:
                import pytz
                tz = pytz.timezone("America/New_York") if self.is_us else pytz.timezone("Asia/Hong_Kong")
            now_t = datetime.now(tz).time()
        open_t = dtime(self.session_open_hour, self.session_open_minute)
        close_t = dtime(self.session_close_hour, self.session_close_minute)
        allow_open = open_t <= now_t < close_t
        must_close = now_t >= close_t
        return allow_open, must_close

    # ────────────────────────────
    #  防御工具
    # ────────────────────────────
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
            return int(BaseStrategy._safe_float(val, default))
        except (ValueError, OverflowError):
            return default

    # ────────────────────────────
    #  通知 / 统计
    # ────────────────────────────
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

    def get_smart_position(self):
        if self.order_manager is not None:
            symbol = self.vt_symbol.split('.')[0]
            return self.order_manager.get_net_qty(symbol)
        return self.pos

    def register_fill_callback(self, callback):
        if self.order_manager is not None:
            symbol = self.vt_symbol.split('.')[0]
            self.order_manager.register_fill_callback(symbol, callback)
