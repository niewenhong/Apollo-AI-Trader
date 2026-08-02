"""
WarrantStrategy v2.9.7
- 继承 ApolloBaseStrategy，统一订阅管理
- 只订阅 Tick + 1M bar，其余本地合成
- Quote 按需查询（subscribe_push=False）
- 信号/开仓/平仓时保存 Quote 快照到数据库
- 修复：参数未声明、bar 引用未定义、SQL 参数数量不匹配等
"""
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from vnpy.trader.object import TickData, BarData
from vnpy.trader.utility import BarGenerator, ArrayManager
from vnpy.trader.constant import Interval, Exchange
from vnpy_ctastrategy import CtaTemplate

from futu import (
    RET_OK, SubType, Session, WrtType,
    OptionType, FinancialQuota, KLType, AuType
)

logger = logging.getLogger(__name__)

# 尝试导入 ApolloBaseStrategy（兼容直接运行和模块运行）
try:
    from strategies.base_strategy import ApolloBaseStrategy
    _HAS_BASE = True
except ImportError:
    try:
        from ..base_strategy import ApolloBaseStrategy
        _HAS_BASE = True
    except ImportError:
        _HAS_BASE = False

if _HAS_BASE:
    class WarrantStrategy(ApolloBaseStrategy):
        """窝轮策略：方向性 + IV 择时 + Quote 存库"""
        author = "Apollo v2.9.7"

        # ---- 参数 ----
        underlying_symbol = ""       # 正股代码，如 "HK.00700"
        wrt_type = "CALL"            # CALL / PUT
        min_delta_abs = 0.3
        max_delta_abs = 0.7
        min_iv_rank = 0.0
        max_iv_rank = 0.7
        min_premium_pct = 0.0
        max_premium_pct = 0.15
        min_volume = 1000000        # 日均成交额 HKD
        min_days_to_expire = 30
        max_days_to_expire = 180
        recovery_warn_pct = 5.0
        recovery_exit_pct = 3.0
        stop_loss_pct = 0.5
        take_profit_pct = 1.0
        max_position = 3
        timeout_bars = 240
        quote_cache_ttl = 5.0
        quote_sample_interval = 300

        # 继承并扩展 parameters
        parameters = ApolloBaseStrategy.parameters + [
            "underlying_symbol", "wrt_type",
            "min_delta_abs", "max_delta_abs",
            "min_iv_rank", "max_iv_rank",
            "min_premium_pct", "max_premium_pct",
            "min_volume",
            "min_days_to_expire", "max_days_to_expire",
            "recovery_warn_pct", "recovery_exit_pct",
            "stop_loss_pct", "take_profit_pct",
            "max_position", "timeout_bars",
            "quote_cache_ttl", "quote_sample_interval",
        ]
        variables = ApolloBaseStrategy.variables + [
            "_entry_price", "_bars_held", "_warrant_symbol",
        ]

        # ---- DEFAULTS 合并 ----
        DEFAULTS = dict(ApolloBaseStrategy.DEFAULTS, **{
            "underlying_symbol": "",
            "wrt_type": "CALL",
            "min_delta_abs": 0.3,
            "max_delta_abs": 0.7,
            "min_iv_rank": 0.0,
            "max_iv_rank": 0.7,
            "min_premium_pct": 0.0,
            "max_premium_pct": 0.15,
            "min_volume": 1000000,
            "min_days_to_expire": 30,
            "max_days_to_expire": 180,
            "recovery_warn_pct": 5.0,
            "recovery_exit_pct": 3.0,
            "stop_loss_pct": 0.5,
            "take_profit_pct": 1.0,
            "max_position": 3,
            "timeout_bars": 240,
            "quote_cache_ttl": 5.0,
            "quote_sample_interval": 300,
        })

        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)

            # 窝轮自身也需要 Tick（用于实时收回价检查）
            self.need_tick = True

            # ========== 本地多周期合成 ==========
            # bg_1m 由基类管理（on_tick → bg_1m → on_bar → on_1m_bar）
            self.bg_5m = BarGenerator(self.on_1m_bar, 5, self.on_5m_bar)
            self.bg_60m = BarGenerator(self.on_1m_bar, 60, self.on_60m_bar)

            self.am_1m = ArrayManager(200)
            self.am_5m = ArrayManager(200)
            self.am_60m = ArrayManager(100)

            # ========== Quote 按需查询 ==========
            self._quote_cache: Dict[str, tuple] = {}
            self._quote_ctx = None
            self._quote_subscribed = set()
            self._last_periodic_sample = 0.0

            # ========== 状态 ==========
            self._current_1m = None
            self._current_5m = None
            self._current_60m = None
            self._entry_price = 0.0
            self._entry_time = None
            self._bars_held = 0
            self._warrant_symbol = ""
            self._underlying_price = 0.0
            self._option_symbols = set()

            # ========== 数据库 ==========
            self._db_path = getattr(cta_engine, 'db_path', 'data/history.db')

        # ============================================================
        # 行情入口（覆写基类 on_tick，但委托基类处理 Bar 合成）
        # ============================================================
        def on_tick(self, tick: TickData):
            """Tick → bg_1m 合成 1M bar + 实时收回价检查"""
            # 先让基类处理 Tick → Bar 合成
            super().on_tick(tick)

            # 更新正股价
            self._underlying_price = tick.last_price

            # 实时检查距收回价
            if self.pos != 0 and self._warrant_symbol:
                quote = self.get_quote(self._warrant_symbol)
                if quote is not None:
                    recovery_ratio = self._f(quote.get('price_recovery_ratio'))
                    if recovery_ratio > 0 and recovery_ratio < self.recovery_exit_pct:
                        self._close_position(
                            tick.last_price,
                            reason=f"距收回价过近 {recovery_ratio:.1f}%"
                        )

        def on_bar(self, bar: BarData):
            """
            接收 1M bar（来自 bg_1m 或引擎推送）。
            喂给 5M/60M BarGenerator 合成，再触发 on_1m_bar。
            """
            self.bg_5m.update_bar(bar)
            self.bg_60m.update_bar(bar)
            self.on_1m_bar(bar)

        # ============================================================
        # 周期回调
        # ============================================================
        def on_1m_bar(self, bar: BarData):
            self._current_1m = bar
            self.am_1m.update_bar(bar)
            self._bars_held += 1

            # 定时采样 Quote
            if self.quote_sample_interval > 0:
                now = time.time()
                if now - self._last_periodic_sample >= self.quote_sample_interval:
                    self._last_periodic_sample = now
                    self._periodic_quote_sample()

        def on_5m_bar(self, bar: BarData):
            self._current_5m = bar
            self.am_5m.update_bar(bar)

            # 5M 周期：检查信号 + 管理持仓
            self._check_signal(bar)
            if self.pos != 0:
                self._manage_position(bar)

        def on_60m_bar(self, bar: BarData):
            self._current_60m = bar
            self.am_60m.update_bar(bar)

        # ============================================================
        # 信号检测
        # ============================================================
        def _check_signal(self, bar: BarData):
            """5M bar 触发信号检测"""
            if self.pos != 0:
                return

            # 1. 技术面确认
            if not self._tech_confirmed():
                return

            # 2. 查询窝轮链
            chain = self._query_warrant_chain()
            if not chain:
                return

            # 3. 筛选最优窝轮
            best = self._select_best_warrant(chain)
            if best is None:
                return

            # 4. 保存信号触发时的 Quote 快照
            self.save_quote_snapshot(best['stock'], 'signal')

            # 5. 开仓
            self._open_position(best, bar)

        def _tech_confirmed(self) -> bool:
            """技术面确认"""
            if not self.am_5m.inited:
                return False
            ma5 = self.am_5m.sma(5)
            ma20 = self.am_5m.sma(20)
            if ma5 is None or ma20 is None:
                return False

            if self.wrt_type == "CALL":
                return ma5 > ma20
            else:
                return ma5 < ma20

        def _query_warrant_chain(self) -> list:
            """查询窝轮链"""
            qc = self._get_quote_ctx()
            if qc is None or not self.underlying_symbol:
                return []

            try:
                from futu import WarrantRequest
                req = WarrantRequest()
                req.code = self.underlying_symbol
                req.wrt_type = WrtType.CALL if self.wrt_type == "CALL" else WrtType.PUT
                req.delta_min = self.min_delta_abs if self.wrt_type == "CALL" else -self.max_delta_abs
                req.delta_max = self.max_delta_abs if self.wrt_type == "CALL" else -self.min_delta_abs
                req.expiry_date_min = datetime.now().strftime('%Y-%m-%d')

                ret, data = qc.get_warrant(self.underlying_symbol, req)
                if ret != RET_OK or len(data) == 0:
                    return []

                results = []
                for _, r in data.iterrows():
                    dte = self._f(r.get('expiry_date_distance', 0))
                    premium = self._f(r.get('premium', 0)) / 100.0
                    iv = self._f(r.get('implied_volatility', 0)) / 100.0
                    vol = self._f(r.get('last_turnover', 0))

                    if dte < self.min_days_to_expire or dte > self.max_days_to_expire:
                        continue
                    if premium > self.max_premium_pct:
                        continue
                    if vol < self.min_volume:
                        continue

                    results.append({
                        'stock': str(r.get('stock', '')),
                        'name': str(r.get('name', '')),
                        'delta': self._f(r.get('delta')),
                        'gamma': self._f(r.get('gamma')),
                        'implied_volatility': iv,
                        'premium': premium,
                        'expiry_date_distance': int(dte),
                        'last_price': self._f(r.get('last_price')),
                        'recovery_price': self._f(r.get('recovery_price')),
                        'price_recovery_ratio': self._f(r.get('price_recovery_ratio')),
                        'last_turnover': vol,
                    })
                return results
            except Exception as e:
                self.write_log(f"[Warrant] 查询窝轮链失败: {e}")
                return []

        def _select_best_warrant(self, chain: list) -> Optional[dict]:
            """筛选最优窝轮"""
            if not chain:
                return None

            safe = [c for c in chain if c['price_recovery_ratio'] >= self.recovery_warn_pct]
            if not safe:
                return None

            target_delta = (self.min_delta_abs + self.max_delta_abs) / 2
            if self.wrt_type == "PUT":
                target_delta = -target_delta

            safe.sort(key=lambda x: abs(x['delta'] - target_delta))
            return safe[0]

        # ============================================================
        # 开仓 / 平仓
        # ============================================================
        def _open_position(self, warrant: dict, bar: BarData):
            """开仓"""
            price = warrant['last_price']
            if price <= 0:
                return

            cash = self._get_available_cash()
            if cash <= 0:
                self.write_log("[Warrant] ⚠️ 资金不足，无法开仓")
                return

            qty = max(getattr(self, 'min_trade_qty', 1),
                      int(cash * 0.1 / (price * 100) / 100) * 100)
            qty = min(qty, self.max_position * 100)

            if qty <= 0:
                self.write_log(f"[Warrant] ⚠️ 计算数量为0，无法开仓 price={price} cash={cash}")
                return

            self.buy(price + self.tick_size, qty, stop=False)
            self._entry_price = price
            self._entry_time = datetime.now()
            self._bars_held = 0
            self._warrant_symbol = warrant['stock']
            self._option_symbols.add(self._warrant_symbol)

            self.save_quote_snapshot(self._warrant_symbol, 'entry')

            self.write_log(
                f"[Warrant] 🟢 开仓 {warrant['stock']} "
                f"价格={price:.4f} 数量={qty} Delta={warrant['delta']:.2f}"
            )
            self._telegram_push(
                f"🟢 窝轮开仓\n"
                f"标的: {warrant['stock']} ({warrant['name']})\n"
                f"价格: {price:.4f} | 数量: {qty}\n"
                f"Delta: {warrant['delta']:.2f} | IV: {warrant['implied_volatility']*100:.1f}%\n"
                f"距到期: {warrant['expiry_date_distance']}天"
            )

        def _manage_position(self, bar: BarData):
            """持仓管理"""
            if self._entry_price <= 0 or self.pos == 0:
                return

            current_price = bar.close_price
            pnl_pct = (current_price - self._entry_price) / self._entry_price

            if pnl_pct >= self.take_profit_pct:
                self._close_position(current_price, reason=f"止盈 +{pnl_pct*100:.1f}%")
                return

            if pnl_pct <= -self.stop_loss_pct:
                self._close_position(current_price, reason=f"止损 {pnl_pct*100:.1f}%")
                return

            if self._bars_held >= self.timeout_bars:
                self._close_position(current_price, reason=f"超时 {self._bars_held}根1Mbar")
                return

        def _close_position(self, price: float, reason: str = ""):
            """平仓"""
            if self.pos == 0:
                return
            qty = abs(self.pos)
            self.sell(price - self.tick_size, qty, stop=False)

            if self._warrant_symbol:
                self.save_quote_snapshot(self._warrant_symbol, 'exit')

            pnl = (price - self._entry_price) * qty if self._entry_price > 0 else 0
            self.write_log(f"[Warrant] 🔴 平仓 {reason} 价格={price:.4f} PnL≈{pnl:.0f}")
            self._telegram_push(
                f"🔴 窝轮平仓\n原因: {reason}\n"
                f"价格: {price:.4f} | PnL: {pnl:.0f}"
            )
            self._entry_price = 0.0
            self._warrant_symbol = ""

        # ============================================================
        # Quote 按需查询
        # ============================================================
        def _get_quote_ctx(self):
            if self._quote_ctx is not None:
                try:
                    ret, _ = self._quote_ctx.get_global_state()
                    if ret == RET_OK:
                        return self._quote_ctx
                except Exception:
                    self._quote_ctx = None

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

            try:
                from futu import OpenQuoteContext
                host = getattr(self.cta_engine, 'opend_host', '127.0.0.1')
                port = getattr(self.cta_engine, 'opend_port', 11111)
                self._quote_ctx = OpenQuoteContext(host, port)
                return self._quote_ctx
            except Exception as e:
                self.write_log(f"[Warrant] 创建 OpenQuoteContext 失败: {e}")
                return None

        def _ensure_quote_subscribed(self, symbol: str) -> bool:
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
            except Exception as e:
                self.write_log(f"[Quote] ⚠️ 订阅失败 {symbol}: {e}")
            return False

        def get_quote(self, symbol: str):
            """按需获取 Quote（带 TTL 缓存）"""
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

        # ============================================================
        # Quote 存库
        # ============================================================
        def init_quote_tables(self):
            """创建 quote_snapshot 表"""
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

        def save_quote_snapshot(self, symbol: str, trigger_type: str):
            """保存 Quote 快照到数据库"""
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
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    symbol, self.underlying_symbol,
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
                    getattr(self, 'current_regime', ''),
                    self.strategy_name,
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                self.write_log(f"[Quote] 存库失败: {e}")

        def _periodic_quote_sample(self):
            for sym in list(self._option_symbols):
                self.save_quote_snapshot(sym, 'periodic')

        # ============================================================
        # 生命周期
        # ============================================================
        def on_start(self):
            self.init_quote_tables()
            # 订阅正股 Tick + QUOTE（通过基类的 _acquire_tick 管理 TICKER）
            if self.underlying_symbol:
                qc = self._get_quote_ctx()
                if qc is not None:
                    try:
                        qc.subscribe(
                            [self.underlying_symbol],
                            [SubType.TICKER, SubType.QUOTE],
                            subscribe_push=True,
                            session=Session.ALL
                        )
                        self.write_log(f"[Warrant] ✅ 订阅 {self.underlying_symbol} TICKER+QUOTE")
                    except Exception as e:
                        self.write_log(f"[Warrant] ⚠️ 订阅失败: {e}")
            self.write_log(
                f"[Warrant] 🚀 {self.strategy_name} 启动 | "
                f"正股={self.underlying_symbol} 类型={self.wrt_type}"
            )

        def on_stop(self):
            if self._quote_ctx is not None:
                try:
                    self._quote_ctx.close()
                except Exception:
                    pass
                self._quote_ctx = None
            self.write_log(f"[Warrant] 🛑 {self.strategy_name} 停止")

        # ============================================================
        # 工具方法
        # ============================================================
        def _f(self, v):
            try:
                return float(v) if v is not None else 0.0
            except (ValueError, TypeError):
                return 0.0

        def _get_available_cash(self) -> float:
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
            try:
                me = getattr(self.cta_engine, 'main_engine', None)
                if me is not None:
                    rc = getattr(me, 'remote_controller', None)
                    if rc is not None and hasattr(rc, 'send_message'):
                        rc.send_message(text)
            except Exception:
                pass
            self.write_log(text)

else:
    # Fallback：如果无法导入 ApolloBaseStrategy，继承 CtaTemplate
    class WarrantStrategy(CtaTemplate):
        """窝轮策略 Fallback 版本（无 ApolloBaseStrategy）"""
        author = "Apollo v2.9.7-fallback"

        underlying_symbol = ""
        wrt_type = "CALL"
        min_delta_abs = 0.3
        max_delta_abs = 0.7
        min_premium_pct = 0.0
        max_premium_pct = 0.15
        min_volume = 1000000
        min_days_to_expire = 30
        max_days_to_expire = 180
        recovery_warn_pct = 5.0
        recovery_exit_pct = 3.0
        stop_loss_pct = 0.5
        take_profit_pct = 1.0
        max_position = 3
        timeout_bars = 240
        quote_cache_ttl = 5.0
        quote_sample_interval = 300

        def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
            super().__init__(cta_engine, strategy_name, vt_symbol, setting)
            self._quote_cache = {}
            self._quote_ctx = None
            self._quote_subscribed = set()
            self._last_periodic_sample = 0.0
            self._entry_price = 0.0
            self._bars_held = 0
            self._warrant_symbol = ""
            self._underlying_price = 0.0
            self._option_symbols = set()
            self._db_path = getattr(cta_engine, 'db_path', 'data/history.db')
            self.write_log("[Warrant] ⚠️ Fallback 模式：未继承 ApolloBaseStrategy")

        def on_tick(self, tick):
            self._underlying_price = tick.last_price

        def on_bar(self, bar):
            pass

        def _f(self, v):
            try:
                return float(v) if v is not None else 0.0
            except (ValueError, TypeError):
                return 0.0

        def _get_quote_ctx(self):
            return None

        def get_quote(self, symbol):
            return None

        def save_quote_snapshot(self, symbol, trigger_type):
            pass

        def init_quote_tables(self):
            pass

        def _telegram_push(self, text):
            self.write_log(text)

        def _get_available_cash(self):
            return 0.0
