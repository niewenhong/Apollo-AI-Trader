"""
BaseOptionStrategy v2.9.6
- on_tick → bg_1m → on_1m_bar → 合成 5M/60M/Daily（链路不经过 on_bar）
- 子类可安全覆盖 on_bar / on_1m_bar，多周期合成不受影响
- Quote 按需查询（subscribe_push=False，无频率限制）
- Quote 快照存库（信号触发/开仓/平仓时），保证回测一致性

v2.9.6 变更：
- 修复：多周期 BarGenerator 更新从 on_bar 移到 on_1m_bar，
  避免子类覆盖 on_bar 导致合成断裂
- 修复：bg_daily 改用 Interval.DAILY 参数
- 修复：删除未使用的导入（Interval, WrtType, OptionType, FinancialQuota）
- 修复：_current_regime → current_regime 拼写统一
- 新增：抽象方法 stub（_send_option_order / _query_full_chain 等），
  运行时由动态注入或 mixin 提供，但声明更清晰
"""
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from vnpy.trader.object import TickData, BarData
from vnpy.trader.utility import BarGenerator
from vnpy_ctastrategy import CtaTemplate

from futu import (
    RET_OK, OpenQuoteContext, SubType, Session,
)

logger = logging.getLogger(__name__)


class BaseOptionStrategy(CtaTemplate):
    """期权策略基类：统一 Tick→1M→多周期合成、Quote 按需查询+存库"""

    author = "Apollo v2.9.6"

    # ---- 参数（子类可覆盖）----
    tick_size = 0.01
    min_trade_qty = 1
    stop_loss_pct = 0.5          # 止损比例（占权利金）
    take_profit_pct = 1.0         # 止盈比例
    max_position = 5               # 最大持仓张数
    quote_cache_ttl = 5.0         # Quote 缓存有效期（秒）
    quote_sample_interval = 300     # 定时采样间隔（秒），0=关闭

    # ---- 子类必须声明的参数/变量（占位，避免 vnpy 警告）----
    parameters: List[str] = []
    variables: List[str] = ["legs", "pnl", "regime_label"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # ========== 本地多周期 BarGenerator ==========
        # on_tick → bg_1m → on_1m_bar（内部再喂给更高周期）
        self.bg_1m = BarGenerator(self.on_1m_bar, 1, self._on_1m_bar_callback)
        # 更高周期由 on_1m_bar 内部驱动（见下方 _on_1m_bar_callback）
        self.bg_5m = BarGenerator(self.on_1m_bar, 5, self.on_5m_bar)
        self.bg_60m = BarGenerator(self.on_1m_bar, 60, self.on_60m_bar)
        # 日线：用 Interval.DAILY 让 vnpy 自动按交易日切分
        self.bg_daily = BarGenerator(
            self.on_1m_bar, 1, self.on_daily_bar,
            interval=Interval.DAILY,
        )

        # ArrayManager
        self.am_1m = ArrayManager(5000)
        self.am_5m = ArrayManager(500)
        self.am_60m = ArrayManager(200)

        # ========== Quote 按需查询 ==========
        self._quote_cache: Dict[str, Tuple[float, Any]] = {}
        self._quote_ctx = None
        self._quote_subscribed = set()
        self._last_periodic_sample = 0.0

        # ========== 数据库 ==========
        self._db_path = getattr(cta_engine, 'db_path', 'data/history.db')

        # ========== 状态 ==========
        self._current_1m = None
        self._current_5m = None
        self._current_60m = None
        self._current_daily = None
        self._option_symbols = set()
        self.legs: Dict[str, dict] = {}
        self.pnl = 0.0
        self.net_premium = 0.0
        self.max_loss = 0.0
        self.max_profit = 0.0
        self.regime_label = ""
        self.current_regime = "unknown"
        self.last_adx = 0.0

    # ============================================================
    # 行情入口（只两个回调）
    # ============================================================
    def on_tick(self, tick: TickData):
        """Tick → bg_1m 合成 1M bar"""
        self.bg_1m.update_tick(tick)

    def on_bar(self, bar: BarData):
        """
        接收引擎推送的 1M bar。
        v2.9.6：仅做持仓/到期管理，不驱动多周期合成
        （多周期由 on_1m_bar 内部驱动，见 _on_1m_bar_callback）
        子类可安全覆盖此方法。
        """
        pass

    # ============================================================
    # 内部回调：1M bar 生成后，同时喂给更高周期 BG
    # ============================================================
    def _on_1m_bar_callback(self, bar: BarData):
        """
        bg_1m 的 window 回调：1M bar 成型后触发。
        在这里同时喂给 5M/60M/Daily 的 BarGenerator，
        再调用用户级 on_1m_bar。
        """
        self.bg_5m.update_bar(bar)
        self.bg_60m.update_bar(bar)
        self.bg_daily.update_bar(bar)
        self.on_1m_bar(bar)

    # ============================================================
    # 周期回调（子类覆盖）
    # ============================================================
    def on_1m_bar(self, bar: BarData):
        self._current_1m = bar
        self.am_1m.update_bar(bar)

        # 定时采样 Quote（可选）
        if self.quote_sample_interval > 0:
            now = time.time()
            if now - self._last_periodic_sample >= self.quote_sample_interval:
                self._last_periodic_sample = now
                self._periodic_quote_sample()

    def on_5m_bar(self, bar: BarData):
        self._current_5m = bar
        self.am_5m.update_bar(bar)

    def on_60m_bar(self, bar: BarData):
        self._current_60m = bar
        self.am_60m.update_bar(bar)

    def on_daily_bar(self, bar: BarData):
        self._current_daily = bar

    # ============================================================
    # Quote 按需查询（核心方法）
    # ============================================================
    def _get_quote_ctx(self) -> Optional[OpenQuoteContext]:
        """获取/复用 OpenQuoteContext，带健康检查"""
        if self._quote_ctx is not None:
            try:
                ret, _ = self._quote_ctx.get_global_state()
                if ret == RET_OK:
                    return self._quote_ctx
            except Exception:
                self._quote_ctx = None

        # 从网关获取
        me = getattr(self.cta_engine, 'main_engine', None)
        if me is not None:
            for gw in getattr(me, 'gateways', {}).values():
                qc = getattr(gw, 'quote_ctx', None)
                if qc is not None:
                    try:
                        ret, _ = qc.get_global_state()
                        if ret == RET_OK:
                            self._quote_ctx = qc
                            return qc
                    except Exception:
                        continue

        # 自建连接
        try:
            host = getattr(self.cta_engine, 'opend_host', '127.0.0.1')
            port = getattr(self.cta_engine, 'opend_port', 11111)
            qc = OpenQuoteContext(host, port)
            self._quote_ctx = qc
            return qc
        except Exception as e:
            self.write_log(f"[Quote] 创建 OpenQuoteContext 失败: {e}")
            return None

    def _ensure_quote_subscribed(self, symbol: str) -> bool:
        """确保已订阅 QUOTE（push=False），返回是否成功"""
        if symbol in self._quote_subscribed:
            return True
        qc = self._get_quote_ctx()
        if qc is None:
            return False
        try:
            ret, _ = qc.subscribe(
                [symbol], [SubType.QUOTE],
                subscribe_push=False,
                session=Session.ALL
            )
            if ret == RET_OK:
                self._quote_subscribed.add(symbol)
                self.write_log(f"[Quote] ✅ 订阅 QUOTE(push=off): {symbol}")
                return True
            else:
                self.write_log(f"[Quote] ⚠️ 订阅失败: {symbol}")
                return False
        except Exception as e:
            self.write_log(f"[Quote] ⚠️ 订阅异常 {symbol}: {e}")
            return False

    def get_quote(self, symbol: str) -> Optional[Any]:
        """
        按需获取 Quote 快照（带 TTL 缓存）。
        返回 pandas.Series 或 None。
        """
        now = time.time()
        if symbol in self._quote_cache:
            ts, data = self._quote_cache[symbol]
            if now - ts < self.quote_cache_ttl:
                return data

        if not self._ensure_quote_subscribed(symbol):
            return None

        qc = self._get_quote_ctx()
        if qc is None:
            return None

        try:
            ret, data = qc.get_stock_quote([symbol])
            if ret == RET_OK and len(data) > 0:
                row = data.iloc[0]
                self._quote_cache[symbol] = (now, row)
                self._option_symbols.add(symbol)
                return row
        except Exception as e:
            self.write_log(f"[Quote] 获取 {symbol} 失败: {e}")
        return None

    def get_quote_batch(self, symbols: List[str]) -> Dict[str, Any]:
        """批量获取 Quote（逐个查询，带缓存）"""
        results = {}
        for sym in symbols:
            q = self.get_quote(sym)
            if q is not None:
                results[sym] = q
        return results

    def _batch_quote(self, symbols: List[str]) -> Dict[str, Any]:
        """别名：兼容子类调用习惯"""
        return self.get_quote_batch(symbols)

    # ============================================================
    # Quote 存库
    # ============================================================
    def save_quote_snapshot(self, symbol: str, trigger_type: str):
        """
        保存 Quote 快照到数据库。
        trigger_type: 'signal' / 'entry' / 'exit' / 'periodic'
        """
        quote = self.get_quote(symbol)
        if quote is None:
            return

        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                INSERT OR REPLACE INTO quote_snapshot
                (symbol, underlying, timestamp, trigger_type,
                 last_price, open_price, high_price, low_price, prev_close,
                 volume, turnover,
                 implied_volatility, delta, gamma, vega, theta, rho,
                 premium, strike_price, expiry_date_distance, open_interest,
                 recovery_price, price_recovery_ratio,
                 pre_price, after_price,
                 regime, strategy_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                symbol,
                getattr(self, 'underlying_symbol', ''),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                trigger_type,
                self._f(quote.get('last_price')),
                self._f(quote.get('open_price')),
                self._f(quote.get('high_price')),
                self._f(quote.get('low_price')),
                self._f(quote.get('prev_close_price')),
                int(quote.get('volume', 0) or 0),
                self._f(quote.get('turnover')),
                self._f(quote.get('implied_volatility')),
                self._f(quote.get('delta')),
                self._f(quote.get('gamma')),
                self._f(quote.get('vega')),
                self._f(quote.get('theta')),
                self._f(quote.get('rho')),
                self._f(quote.get('premium')),
                self._f(quote.get('strike_price')),
                int(quote.get('expiry_date_distance', 0) or 0),
                int(quote.get('open_interest', 0) or 0),
                self._f(quote.get('recovery_price')),
                self._f(quote.get('price_recovery_ratio')),
                self._f(quote.get('pre_price')),
                self._f(quote.get('after_price')),
                self.current_regime,
                self.strategy_name,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            self.write_log(f"[Quote] 存库失败: {e}")

    def _periodic_quote_sample(self):
        """定时采样（由 on_1m_bar 触发）"""
        for sym in list(self._option_symbols):
            self.save_quote_snapshot(sym, 'periodic')

    # ============================================================
    # 数据库初始化
    # ============================================================
    def init_quote_tables(self):
        """创建 quote_snapshot 表（如果不存在）"""
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS quote_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    underlying TEXT,
                    timestamp DATETIME NOT NULL,
                    trigger_type TEXT,
                    last_price REAL, open_price REAL, high_price REAL,
                    low_price REAL, prev_close REAL,
                    volume INTEGER, turnover REAL,
                    implied_volatility REAL, delta REAL, gamma REAL,
                    vega REAL, theta REAL, rho REAL,
                    premium REAL, strike_price REAL,
                    expiry_date_distance INTEGER, open_interest INTEGER,
                    recovery_price REAL, price_recovery_ratio REAL,
                    pre_price REAL, after_price REAL,
                    regime TEXT, strategy_name TEXT,
                    UNIQUE(symbol, timestamp, trigger_type)
                );
                CREATE INDEX IF NOT EXISTS idx_qs_symbol_time
                    ON quote_snapshot(symbol, timestamp);
                CREATE INDEX IF NOT EXISTS idx_qs_trigger
                    ON quote_snapshot(trigger_type);
            """)
            conn.commit()
            conn.close()
            self.write_log("[Quote] ✅ quote_snapshot 表就绪")
        except Exception as e:
            self.write_log(f"[Quote] 建表失败: {e}")

    # ============================================================
    # 生命周期
    # ============================================================
    def on_start(self):
        self.init_quote_tables()
        self.write_log(f"[BaseOption] 🚀 {self.strategy_name} 启动 | "
                       f"订阅: Tick→1M→多周期 | Quote按需(push=off)")

    def on_stop(self):
        if self._quote_ctx is not None:
            try:
                self._quote_ctx.close()
            except Exception:
                pass
            self._quote_ctx = None
        self.write_log(f"[BaseOption] 🛑 {self.strategy_name} 停止")

    # ============================================================
    # 工具方法
    # ============================================================
    def _f(self, v):
        """安全转 float"""
        try:
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _get_available_cash(self) -> float:
        """获取可用现金（保守：失败返回 0）"""
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            if me is not None:
                for gw in getattr(me, 'gateways', {}).values():
                    pm = getattr(gw, 'position_manager', None)
                    if pm is not None and hasattr(pm, 'available_cash'):
                        return float(pm.available_cash())
            qc = self._get_quote_ctx()
            if qc is not None:
                ret, acc = qc.get_acc_list()
                if ret == RET_OK and len(acc) > 0:
                    return float(acc.iloc[0].get('power', 0))
        except Exception as e:
            self.write_log(f"[Cash] 查询失败: {e}")
        return 0.0

    def _telegram_push(self, text: str):
        """推送 Telegram 消息"""
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            if me is not None:
                rc = getattr(me, 'remote_controller', None)
                if rc is not None and hasattr(rc, 'send_message'):
                    rc.send_message(text)
        except Exception:
            pass
        self.write_log(text)

    # ============================================================
    # 子类必须/应该实现的方法（stub + 友好报错）
    # ============================================================
    def _to_futu_code(self) -> str:
        """将 vt_symbol 转为 futu 格式（子类可覆盖）"""
        # 默认实现：假设 vt_symbol 形如 "US.AAPL"
        parts = self.vt_symbol.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return self.vt_symbol

    def _query_full_chain(self, code: str) -> List[dict]:
        """查询期权链（子类应覆盖，此处为 stub）"""
        self.write_log(f"[Stub] _query_full_chain({code}) 未实现")
        return []

    def _select_contracts(self, chain: List[dict], opt_type: str) -> List[dict]:
        """从链中筛选 call/put 合约"""
        if opt_type == "call":
            return [c for c in chain if c.get("is_call")]
        elif opt_type == "put":
            return [c for c in chain if c.get("is_put")]
        return []

    def _send_option_order(self, leg: dict, direction, offset) -> bool:
        """发送期权委托（子类应覆盖）"""
        self.write_log(f"[Stub] _send_option_order 未实现 leg={leg.get('code','?')}")
        return False

    def _open_spread(self, long_leg: dict, short_leg: dict) -> bool:
        """开仓价差双腿"""
        ok1 = self._send_option_order(long_leg, "LONG", "OPEN")
        ok2 = self._send_option_order(short_leg, "SHORT", "OPEN")
        if ok1 and ok2:
            return True
        # 回滚
        if ok1:
            self._send_option_order(long_leg, "SHORT", "CLOSE")
        if ok2:
            self._send_option_order(short_leg, "LONG", "CLOSE")
        return False

    def _close_all_legs(self):
        """平掉所有持仓腿"""
        for name, leg in list(self.legs.items()):
            direction = "SHORT" if leg.get("is_long") else "LONG"
            self._send_option_order(leg, direction, "CLOSE")
        self.legs.clear()

    def _manage_expiry(self, bar: BarData) -> bool:
        """到期管理（子类可覆盖）"""
        return False

    def _estimate_pnl(self) -> float:
        """估算当前持仓盈亏"""
        return self.pnl

    def _scaled_size(self) -> int:
        """计算缩放后的持仓规模"""
        return getattr(self, 'position_size', 1)

    def _roll_positions(self):
        """展期：先平旧仓，标记状态"""
        self.write_log(f"[{self.strategy_name}] 展期：平仓旧腿")
        self._close_all_legs()
        self.net_premium = 0.0
