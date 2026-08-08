# -*- coding: utf-8 -*-
"""
core/db_manager.py - Apollo Trader v3.8.2 (Fixed + get_order_signals)

基于你提供的 v3.8.2 版本，仅新增：
- get_order_signals()
- get_order_signals_recent()

其余完全保留，不删除任何已有功能。
"""
import json
import logging
import uuid
import hashlib
import threading
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import NullPool

logger = logging.getLogger("DBManager")


class DBManager:
    """数据库管理器 v3.8.2 - SQLAlchemy 后端"""
    _instance = None
    _initialized = False
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_url: str = "sqlite:///data/apollo.db"):
        if not db_url.startswith("sqlite://") and not db_url.startswith("postgresql://"):
            db_url = f"sqlite:///{db_url}"
        if DBManager._initialized:
            return
        with DBManager._init_lock:
            if DBManager._initialized:
                return
            self.db_url = db_url
            if db_url.startswith("sqlite"):
                self.engine = create_engine(
                    db_url,
                    poolclass=NullPool,
                    connect_args={"check_same_thread": False},
                    echo=False,
                )
            else:
                self.engine = create_engine(
                    db_url,
                    pool_size=5,
                    max_overflow=10,
                    echo=False,
                )
            self.Session = scoped_session(sessionmaker(bind=self.engine))
            self.lock = threading.Lock()
            self._create_tables()
            self._migrate_all_tables()
            DBManager._initialized = True
            logger.info(f"[DB INIT] ✅ 数据库表创建/验证完成 ({self.db_url})")

    # ==================== 事务辅助 ====================

    def _safe_commit(self):
        pass

    # ==================== 建表 ====================

    def _create_tables(self):
        session = self.Session()
        try:
            # 1. users 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL DEFAULT '',
                    tier TEXT DEFAULT 'FREE',
                    user_id TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    last_login TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1
                )
            """))

            # 2. strategy_config 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT UNIQUE,
                    class_name TEXT,
                    vt_symbol TEXT,
                    market TEXT DEFAULT 'US',
                    params TEXT DEFAULT '{}',
                    enabled INTEGER DEFAULT 1,
                    active INTEGER DEFAULT 1,
                    version INTEGER DEFAULT 1,
                    current_version INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'PENDING',
                    status_msg TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    modifier TEXT DEFAULT '',
                    user_id TEXT DEFAULT 'SYSTEM',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """))

            # 3. tick_data 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS tick_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT, exchange TEXT, datetime TEXT, gateway_name TEXT,
                    name TEXT, last_price REAL, volume REAL, turnover REAL,
                    open_price REAL, high_price REAL, low_price REAL, pre_close REAL,
                    bid_price_1 REAL, ask_price_1 REAL, bid_volume_1 REAL, ask_volume_1 REAL,
                    source TEXT, saved_at_utc TEXT, received_at TEXT
                )
            """))

            # 4. quote_snapshot 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS quote_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT, underlying TEXT, timestamp TEXT, trigger_type TEXT,
                    last_price REAL, open_price REAL, high_price REAL, low_price REAL,
                    prev_close REAL, volume REAL, turnover REAL,
                    implied_volatility REAL, delta REAL, gamma REAL, vega REAL,
                    theta REAL, rho REAL, premium REAL, strike_price REAL,
                    expiry_date_distance REAL, open_interest REAL,
                    recovery_price REAL, price_recovery_ratio REAL,
                    pre_price REAL, after_price REAL, regime TEXT,
                    strategy_name TEXT, extra_json TEXT
                )
            """))

            # 5. ai_stock_pool 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS ai_stock_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    market TEXT DEFAULT 'US',
                    score REAL DEFAULT 0,
                    reason TEXT DEFAULT '',
                    source TEXT DEFAULT 'selector',
                    created_at TEXT DEFAULT '',
                    anomaly_type TEXT DEFAULT 'none',
                    regime TEXT DEFAULT 'range',
                    asset_class TEXT DEFAULT 'EQUITY',
                    extra_json TEXT DEFAULT '{}'
                )
            """))

            # 6. stock_diagnosis 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS stock_diagnosis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    market TEXT DEFAULT 'US',
                    diagnosis TEXT DEFAULT '',
                    score REAL DEFAULT 0,
                    updated_at TEXT DEFAULT ''
                )
            """))

            # 7. deploy_log 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS deploy_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT, version INTEGER, action TEXT,
                    operator TEXT, result TEXT, message TEXT, created_at TEXT DEFAULT ''
                )
            """))

            # 8. events 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, level TEXT, message TEXT,
                    strategy_name TEXT DEFAULT '',
                    user_id TEXT DEFAULT 'SYSTEM'
                )
            """))

            # 9. param_history 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS param_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vt_symbol TEXT, class_name TEXT, params TEXT,
                    version INTEGER, created_at TEXT DEFAULT '',
                    changed_by TEXT DEFAULT 'system', reason TEXT DEFAULT ''
                )
            """))

            # 10. regime_records 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS regime_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'US',
                    regime TEXT,
                    prob_trend REAL DEFAULT 0,
                    prob_range REAL DEFAULT 0,
                    prob_volatile REAL DEFAULT 0,
                    confidence REAL DEFAULT 0,
                    features TEXT DEFAULT '{}',
                    features_json TEXT DEFAULT '{}',
                    timestamp TEXT DEFAULT (datetime('now')),
                    UNIQUE(symbol, exchange)
                )
            """))

            # 11. strategy_runs 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    vt_symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    params_hash TEXT,
                    params_json TEXT DEFAULT '{}',
                    started_at TEXT,
                    ended_at TEXT,
                    status TEXT DEFAULT 'RUNNING',
                    total_pnl REAL DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    avg_win REAL DEFAULT 0,
                    avg_loss REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0,
                    sharpe_ratio REAL DEFAULT 0,
                    profit_factor REAL DEFAULT 0,
                    exit_reason TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    user_id TEXT DEFAULT 'SYSTEM'
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_runs_name ON strategy_runs(strategy_name)"))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_runs_status ON strategy_runs(status)"))

            # 12. strategy_daily_pnl 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_daily_pnl (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    start_equity REAL DEFAULT 0,
                    end_equity REAL DEFAULT 0,
                    daily_pnl REAL DEFAULT 0,
                    daily_trades INTEGER DEFAULT 0,
                    high_water_mark REAL DEFAULT 0,
                    daily_drawdown REAL DEFAULT 0,
                    cumulative_pnl REAL DEFAULT 0,
                    UNIQUE(run_id, trade_date)
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_daily_name ON strategy_daily_pnl(strategy_name)"))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_daily_run ON strategy_daily_pnl(run_id)"))

            # 13. performance_snapshot 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS performance_snapshot (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    run_id TEXT,
                    captured_at TEXT DEFAULT '',
                    total_pnl REAL DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    avg_win REAL DEFAULT 0,
                    avg_loss REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0,
                    current_drawdown REAL DEFAULT 0,
                    sharpe_ratio REAL DEFAULT 0,
                    profit_factor REAL DEFAULT 0,
                    open_positions INTEGER DEFAULT 0,
                    notes TEXT DEFAULT ''
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_perf_name ON performance_snapshot(strategy_name)"))

            # 14. strategy_history 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    class_name TEXT,
                    vt_symbol TEXT,
                    market TEXT,
                    params TEXT DEFAULT '{}',
                    status TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    total_pnl REAL DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    removed_by TEXT,
                    remove_reason TEXT,
                    removed_at TEXT,
                    user_id TEXT DEFAULT 'SYSTEM'
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_hist_name ON strategy_history(strategy_name)"))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_hist_market ON strategy_history(market)"))

            # 15. shared_strategies 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS shared_strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    share_id TEXT UNIQUE NOT NULL,
                    strategy_name TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    shared_with TEXT DEFAULT '*',
                    params TEXT DEFAULT '{}',
                    vt_symbol TEXT,
                    market TEXT,
                    class_name TEXT,
                    shared_at TEXT DEFAULT '',
                    expires_at TEXT,
                    status TEXT DEFAULT 'ACTIVE'
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_share_owner ON shared_strategies(owner_user_id)"))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_share_with ON shared_strategies(shared_with)"))

            # 16. user_equity 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS user_equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    equity REAL DEFAULT 0,
                    cash REAL DEFAULT 0,
                    market_val REAL DEFAULT 0,
                    frozen REAL DEFAULT 0,
                    power REAL DEFAULT 0,
                    currency TEXT DEFAULT 'USD',
                    recorded_at TEXT DEFAULT ''
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_equity_user ON user_equity(user_id)"))

            # 17. order_signals 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS order_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT UNIQUE NOT NULL,
                    user_id TEXT DEFAULT 'SYSTEM',
                    symbol TEXT NOT NULL,
                    direction TEXT,
                    price REAL,
                    volume INTEGER,
                    offset TEXT DEFAULT 'OPEN',
                    strategy_name TEXT DEFAULT '',
                    status TEXT DEFAULT 'PENDING',
                    submitted_at TEXT DEFAULT '',
                    filled_at TEXT,
                    fill_price REAL,
                    fill_volume INTEGER,
                    commission REAL DEFAULT 0,
                    pnl REAL DEFAULT 0
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_signal_user ON order_signals(user_id)"))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_signal_strategy ON order_signals(strategy_name)"))

            # 18. strategy_params_archive 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS strategy_params_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT,
                    vt_symbol TEXT,
                    class_name TEXT,
                    old_params TEXT DEFAULT '{}',
                    new_params TEXT DEFAULT '{}',
                    changed_by TEXT DEFAULT 'system',
                    reason TEXT DEFAULT '',
                    archived_at TEXT DEFAULT ''
                )
            """))

            # 19. ai_param_suggestions 表
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS ai_param_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vt_symbol TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    suggested_params TEXT DEFAULT '{}',
                    confidence REAL DEFAULT 0,
                    source TEXT DEFAULT 'advisor',
                    created_at TEXT DEFAULT ''
                )
            """))

            # ★ 20. positions 表（用于 AccountManager 持仓同步）
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS positions (
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT,
                    quantity REAL DEFAULT 0,
                    avg_cost REAL DEFAULT 0,
                    last_price REAL DEFAULT 0,
                    pnl REAL DEFAULT 0,
                    is_managed INTEGER DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (user_id, symbol)
                )
            """))

            # ★ 21. dbbardata 表（用于 data_job 统计）
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS dbbardata (
                    datetime TEXT,
                    symbol TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    turnover REAL
                )
            """))

            # ★ 22. trade_log 表（用于生命周期衰减检测）
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS trade_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    run_id TEXT,
                    symbol TEXT,
                    direction TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    quantity REAL DEFAULT 0,
                    pnl REAL DEFAULT 0,
                    holding_days INTEGER DEFAULT 0,
                    entry_time TEXT,
                    close_time TEXT,
                    status TEXT DEFAULT 'OPEN',
                    reason TEXT DEFAULT '',
                    created_at TEXT DEFAULT ''
                )
            """))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_tl_name ON trade_log(strategy_name)"))
            session.execute(text("CREATE INDEX IF NOT EXISTS idx_tl_status ON trade_log(status)"))

            session.commit()
            logger.info("[DB INIT] ✅ 全部 22 张表创建/验证完成")
        except Exception as e:
            session.rollback()
            logger.error(f"[DB INIT] 建表失败: {e}")
            raise
        finally:
            self.Session.remove()

    def _migrate_all_tables(self):
        session = self.Session()
        try:
            inspector = inspect(self.engine)

            # strategy_config 迁移
            sc_cols = {col['name'] for col in inspector.get_columns("strategy_config")}
            sc_additions = {
                'status': "ALTER TABLE strategy_config ADD COLUMN status TEXT DEFAULT 'PENDING'",
                'status_msg': "ALTER TABLE strategy_config ADD COLUMN status_msg TEXT DEFAULT ''",
                'enabled': "ALTER TABLE strategy_config ADD COLUMN enabled INTEGER DEFAULT 1",
                'active': "ALTER TABLE strategy_config ADD COLUMN active INTEGER DEFAULT 1",
                'current_version': "ALTER TABLE strategy_config ADD COLUMN current_version INTEGER DEFAULT 1",
                'version': "ALTER TABLE strategy_config ADD COLUMN version INTEGER DEFAULT 1",
                'created_at': "ALTER TABLE strategy_config ADD COLUMN created_at TEXT DEFAULT ''",
                'updated_at': "ALTER TABLE strategy_config ADD COLUMN updated_at TEXT DEFAULT ''",
                'user_id': "ALTER TABLE strategy_config ADD COLUMN user_id TEXT DEFAULT 'SYSTEM'",
            }
            for col, ddl in sc_additions.items():
                if col not in sc_cols:
                    try:
                        session.execute(text(ddl))
                        session.commit()
                        logger.info(f"[DB] 迁移: strategy_config 添加 {col}")
                    except Exception:
                        pass

            # ai_stock_pool 迁移
            ap_cols = {col['name'] for col in inspector.get_columns("ai_stock_pool")}
            ap_additions = {
                'anomaly_type': "ALTER TABLE ai_stock_pool ADD COLUMN anomaly_type TEXT DEFAULT 'none'",
                'regime': "ALTER TABLE ai_stock_pool ADD COLUMN regime TEXT DEFAULT 'range'",
                'asset_class': "ALTER TABLE ai_stock_pool ADD COLUMN asset_class TEXT DEFAULT 'EQUITY'",
                'extra_json': "ALTER TABLE ai_stock_pool ADD COLUMN extra_json TEXT DEFAULT '{}'",
            }
            for col, ddl in ap_additions.items():
                if col not in ap_cols:
                    try:
                        session.execute(text(ddl))
                        session.commit()
                        logger.info(f"[DB] 迁移: ai_stock_pool 添加 {col}")
                    except Exception:
                        pass

            # regime_records 迁移
            rr_cols = {col['name'] for col in inspector.get_columns("regime_records")}
            rr_additions = {
                'exchange': "ALTER TABLE regime_records ADD COLUMN exchange TEXT NOT NULL DEFAULT 'US'",
                'features_json': "ALTER TABLE regime_records ADD COLUMN features_json TEXT DEFAULT '{}'",
            }
            for col, ddl in rr_additions.items():
                if col not in rr_cols:
                    try:
                        session.execute(text(ddl))
                        session.commit()
                        logger.info(f"[DB] 迁移: regime_records 添加 {col}")
                    except Exception:
                        pass

            # stock_diagnosis 迁移
            sd_cols = {col['name'] for col in inspector.get_columns("stock_diagnosis")}
            sd_additions = {
                'market': "ALTER TABLE stock_diagnosis ADD COLUMN market TEXT DEFAULT 'US'",
            }
            for col, ddl in sd_additions.items():
                if col not in sd_cols:
                    try:
                        session.execute(text(ddl))
                        session.commit()
                        logger.info(f"[DB] 迁移: stock_diagnosis 添加 {col}")
                    except Exception:
                        pass

            # events 迁移
            ev_cols = {col['name'] for col in inspector.get_columns("events")}
            ev_additions = {
                'user_id': "ALTER TABLE events ADD COLUMN user_id TEXT DEFAULT 'SYSTEM'",
            }
            for col, ddl in ev_additions.items():
                if col not in ev_cols:
                    try:
                        session.execute(text(ddl))
                        session.commit()
                        logger.info(f"[DB] 迁移: events 添加 {col}")
                    except Exception:
                        pass

            # users 迁移
            try:
                users_cols = {col['name'] for col in inspector.get_columns("users")}
                users_additions = {
                    'password_hash': "ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''",
                    'tier': "ALTER TABLE users ADD COLUMN tier TEXT DEFAULT 'FREE'",
                    'user_id': "ALTER TABLE users ADD COLUMN user_id TEXT DEFAULT ''",
                    'created_at': "ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT ''",
                    'last_login': "ALTER TABLE users ADD COLUMN last_login TEXT DEFAULT ''",
                    'enabled': "ALTER TABLE users ADD COLUMN enabled INTEGER DEFAULT 1",
                }
                for col, ddl in users_additions.items():
                    if col not in users_cols:
                        try:
                            session.execute(text(ddl))
                            session.commit()
                            logger.info(f"[DB] 迁移: users 添加 {col}")
                        except Exception:
                            pass
            except Exception:
                pass

            session.commit()
        except Exception as e:
            logger.warning(f"[DB] 迁移检查失败: {e}")
        finally:
            self.Session.remove()

    # ==================== 工具方法 ====================

    @staticmethod
    def _row_to_dict(row) -> Optional[dict]:
        if row is None:
            return None
        return dict(row._mapping)

    @staticmethod
    def _parse_params(d: dict) -> dict:
        if isinstance(d.get("params"), str):
            try:
                d["params"] = json.loads(d["params"])
            except Exception:
                d["params"] = {}
        return d

    @staticmethod
    def _f(value) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", ""))
            except ValueError:
                return 0.0
        return 0.0

    @staticmethod
    def _serialize(obj) -> str:
        if obj is None:
            return ""
        if isinstance(obj, str):
            return obj
        if isinstance(obj, (int, float, bool)):
            return str(obj)
        try:
            return json.dumps(obj, ensure_ascii=False, default=str)
        except Exception:
            return str(obj)

    # ==================== 用户管理（v3.8.2 修复） ====================

    def user_exists(self, username: str) -> bool:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT 1 FROM users WHERE username = :un"),
                {"un": username}
            ).fetchone()
            return row is not None
        except Exception:
            return False
        finally:
            self.Session.remove()

    def create_user(self, username, password_hash: str = "",
                    tier: str = "FREE", user_id: str = "") -> bool:
        if isinstance(username, dict):
            user_dict = username
            username = user_dict.get("username", "")
            if not password_hash and "password_hash" in user_dict:
                password_hash = user_dict["password_hash"]
            tier = user_dict.get("tier", tier)
            user_id = user_dict.get("user_id", user_id)
            if not user_id or user_id == "":
                user_id = user_dict.get("id", "")

        if not isinstance(username, str):
            try:
                username = str(username)
            except Exception:
                logger.error("[DB] create_user: username 无法转为字符串")
                return False

        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            uid = user_id or f"U{uuid.uuid4().hex[:8]}"

            if isinstance(password_hash, dict):
                if "password_hash" in password_hash:
                    password_hash = password_hash["password_hash"]
                else:
                    password_hash = json.dumps(password_hash, ensure_ascii=False)
            if isinstance(password_hash, bytes):
                password_hash = password_hash.decode('utf-8')
            if not isinstance(password_hash, str):
                password_hash = str(password_hash)
            if not password_hash:
                password_hash = hashlib.sha256(username.encode('utf-8')).hexdigest()

            session.execute(
                text("""
                    INSERT OR IGNORE INTO users
                    (username, password_hash, tier, user_id, created_at, enabled)
                    VALUES (:un, :ph, :t, :uid, :ca, 1)
                """),
                {"un": username, "ph": password_hash, "t": tier,
                 "uid": uid, "ca": now}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] create_user 失败 {username}: {e}")
            return False
        finally:
            self.Session.remove()

    def insert_user(self, username, password_hash: str = "",
                    tier: str = "FREE", user_id: str = "") -> bool:
        return self.create_user(username, password_hash, tier, user_id)

    def get_user(self, username: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT * FROM users WHERE username = :un"),
                {"un": username}
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            self.Session.remove()

    def update_user(self, username: str, **fields) -> bool:
        session = self.Session()
        try:
            allowed = {"password_hash", "tier", "last_login", "enabled"}
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return False
            if "password_hash" in updates:
                ph = updates["password_hash"]
                if isinstance(ph, dict):
                    updates["password_hash"] = json.dumps(ph, ensure_ascii=False)
                elif isinstance(ph, bytes):
                    updates["password_hash"] = ph.decode('utf-8')
                elif not isinstance(ph, str):
                    updates["password_hash"] = str(ph)

            set_clause = ", ".join(f"{k}=:{k}" for k in updates)
            updates["un"] = username
            session.execute(
                text(f"UPDATE users SET {set_clause} WHERE username=:un"),
                updates
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] update_user 失败 {username}: {e}")
            return False
        finally:
            self.Session.remove()

    def delete_user(self, username: str) -> bool:
        session = self.Session()
        try:
            session.execute(
                text("DELETE FROM users WHERE username=:un"),
                {"un": username}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] delete_user 失败 {username}: {e}")
            return False
        finally:
            self.Session.remove()

    def list_users(self, limit: int = 100) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM users ORDER BY created_at DESC LIMIT :lim"),
                {"lim": limit}
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    # ==================== 策略 CRUD ====================

    def get_all_strategies(self, enabled_only: bool = False) -> List[dict]:
        session = self.Session()
        try:
            if enabled_only:
                rows = session.execute(
                    text("SELECT * FROM strategy_config WHERE enabled = 1 ORDER BY updated_at DESC")
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM strategy_config ORDER BY updated_at DESC")
                ).fetchall()
            return [self._parse_params(self._row_to_dict(r)) for r in rows]
        finally:
            self.Session.remove()

    def get_strategy(self, strategy_name: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT * FROM strategy_config WHERE strategy_name = :name"),
                {"name": strategy_name}
            ).fetchone()
            if not row:
                return None
            return self._parse_params(self._row_to_dict(row))
        finally:
            self.Session.remove()

    def save_strategy(self, strategy_name: str, class_name: str,
                      vt_symbol: str, market: str,
                      params: dict, source: str = "", modifier: str = "",
                      user_id: str = "SYSTEM") -> Tuple[bool, int]:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            params_json = json.dumps(params, ensure_ascii=False)
            existing = self.get_strategy(strategy_name)
            if existing:
                new_version = existing.get("current_version", 1) + 1
                session.execute(
                    text("""
                        UPDATE strategy_config SET
                            class_name=:cn, vt_symbol=:vs, market=:m, params=:p,
                            current_version=:cv, version=:v, source=:src, modifier=:mod,
                            user_id=:uid, updated_at=:ua, status='PENDING', enabled=1, active=1
                        WHERE strategy_name=:sn
                    """),
                    {"cn": class_name, "vs": vt_symbol, "m": market, "p": params_json,
                     "cv": new_version, "v": new_version, "src": source, "mod": modifier,
                     "uid": uid, "ua": now, "sn": strategy_name}
                )
                self._save_param_history_internal(session, vt_symbol, class_name, params, new_version, changed_by=modifier)
                session.commit()
                return True, new_version
            else:
                session.execute(
                    text("""
                        INSERT INTO strategy_config
                        (strategy_name, class_name, vt_symbol, market, params,
                         enabled, active, version, current_version, status,
                         source, modifier, user_id, created_at, updated_at)
                        VALUES (:sn, :cn, :vs, :m, :p,
                                1, 1, 1, 1, 'PENDING',
                                :src, :mod, :uid, :ca, :ua)
                    """),
                    {"sn": strategy_name, "cn": class_name, "vs": vt_symbol, "m": market,
                     "p": params_json, "src": source, "mod": modifier,
                     "uid": uid, "ca": now, "ua": now}
                )
                self._save_param_history_internal(session, vt_symbol, class_name, params, 1, changed_by=modifier)
                session.commit()
                return True, 1
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_strategy 失败 {strategy_name}: {e}")
            return False, 0
        finally:
            self.Session.remove()

    def _save_param_history_internal(self, session, vt_symbol, class_name, params, version, changed_by="system"):
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            params_json = json.dumps(params, ensure_ascii=False) if isinstance(params, dict) else str(params)
            session.execute(
                text("""
                    INSERT INTO param_history
                    (vt_symbol, class_name, params, version, created_at, changed_by, reason)
                    VALUES (:vs, :cn, :p, :v, :ca, :cb, '')
                """),
                {"vs": vt_symbol, "cn": class_name, "p": params_json,
                 "v": version, "ca": now, "cb": changed_by}
            )
        except Exception as e:
            logger.debug(f"[DB] param_history 保存失败: {e}")

    def disable_strategy(self, strategy_name: str):
        session = self.Session()
        try:
            session.execute(
                text("UPDATE strategy_config SET enabled=0, active=0, status='DISABLED' WHERE strategy_name=:sn"),
                {"sn": strategy_name}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] disable_strategy 失败 {strategy_name}: {e}")
        finally:
            self.Session.remove()

    def mark_deployed(self, strategy_name: str, version: int, operator: str = "system"):
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("UPDATE strategy_config SET status='RUNNING', updated_at=:ua WHERE strategy_name=:sn"),
                {"ua": now, "sn": strategy_name}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] mark_deployed 失败 {strategy_name}: {e}")
        finally:
            self.Session.remove()

    # ==================== 状态追踪 ====================

    def set_strategy_status(self, strategy_name: str, status: str, msg: str = ""):
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("UPDATE strategy_config SET status=:s, status_msg=:m, updated_at=:ua WHERE strategy_name=:sn"),
                {"s": status, "m": msg, "ua": now, "sn": strategy_name}
            )
            session.commit()
            logger.info(f"[DB] 🏷️ {strategy_name} status → {status}")
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] set_strategy_status 失败 {strategy_name}: {e}")
        finally:
            self.Session.remove()

    def get_strategy_status(self, strategy_name: str) -> Optional[str]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT status FROM strategy_config WHERE strategy_name=:sn"),
                {"sn": strategy_name}
            ).fetchone()
            return row[0] if row else None
        finally:
            self.Session.remove()

    def get_status_summary(self) -> dict:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT status, COUNT(*) as cnt FROM strategy_config GROUP BY status")
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            self.Session.remove()

    def get_active_strategies(self) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM strategy_config WHERE enabled=1 AND active=1 ORDER BY updated_at DESC")
            ).fetchall()
            return [self._parse_params(self._row_to_dict(r)) for r in rows]
        finally:
            self.Session.remove()

    # ==================== 运行生命周期管理 ====================

    def start_run(self, strategy_name: str, class_name: str,
                  vt_symbol: str, market: str,
                  params: dict, user_id: str = "SYSTEM") -> str:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            params_json = json.dumps(params, ensure_ascii=False)
            params_hash = hashlib.md5(params_json.encode()).hexdigest() if isinstance(params, dict) else ""
            self._end_orphan_runs_internal(session, strategy_name, reason="restart")
            run_id = str(uuid.uuid4())[:12]
            session.execute(
                text("""
                    INSERT INTO strategy_runs
                    (run_id, strategy_name, class_name, vt_symbol, market,
                     params_hash, params_json, started_at, status, user_id)
                    VALUES (:rid, :sn, :cn, :vs, :m, :ph, :pj, :sa, 'RUNNING', :uid)
                """),
                {"rid": run_id, "sn": strategy_name, "cn": class_name, "vs": vt_symbol,
                 "m": market, "ph": params_hash, "pj": params_json,
                 "sa": now, "uid": user_id}
            )
            session.commit()
            logger.info(f"[DB] 🏃 {strategy_name} 运行开始 run_id={run_id} user={user_id}")
            return run_id
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] start_run 失败 {strategy_name}: {e}")
            return ""
        finally:
            self.Session.remove()

    def _end_orphan_runs_internal(self, session, strategy_name: str, reason: str = "restart"):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session.execute(
            text("UPDATE strategy_runs SET ended_at=:ea, status='STOPPED', exit_reason=:er "
                 "WHERE strategy_name=:sn AND status='RUNNING'"),
            {"ea": now, "er": reason, "sn": strategy_name}
        )

    def end_run(self, run_id: str, exit_reason: str = "manual",
                perf_data: Optional[dict] = None):
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if perf_data:
                session.execute(
                    text("""
                        UPDATE strategy_runs SET
                            ended_at=:ea, status='STOPPED',
                            total_pnl=:tp, total_trades=:tt,
                            winning_trades=:wt, losing_trades=:lt,
                            win_rate=:wr, avg_win=:aw, avg_loss=:al,
                            max_drawdown=:md, sharpe_ratio=:sr, profit_factor=:pf,
                            exit_reason=:er
                        WHERE run_id=:rid
                    """),
                    {"ea": now, "tp": perf_data.get("total_pnl", 0),
                     "tt": perf_data.get("total_trades", 0),
                     "wt": perf_data.get("winning_trades", 0),
                     "lt": perf_data.get("losing_trades", 0),
                     "wr": perf_data.get("win_rate", 0),
                     "aw": perf_data.get("avg_win", 0),
                     "al": perf_data.get("avg_loss", 0),
                     "md": perf_data.get("max_drawdown", 0),
                     "sr": perf_data.get("sharpe_ratio", 0),
                     "pf": perf_data.get("profit_factor", 0),
                     "er": exit_reason, "rid": run_id}
                )
            else:
                session.execute(
                    text("UPDATE strategy_runs SET ended_at=:ea, status='STOPPED', exit_reason=:er WHERE run_id=:rid"),
                    {"ea": now, "er": exit_reason, "rid": run_id}
                )
            session.commit()
            logger.info(f"[DB] 🔚 run_id={run_id} 已结束 (reason={exit_reason})")
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] end_run 失败 {run_id}: {e}")
        finally:
            self.Session.remove()

    def update_run_performance(self, run_id: str, perf_data: dict):
        session = self.Session()
        try:
            session.execute(
                text("""
                    UPDATE strategy_runs SET
                        total_pnl=:tp, total_trades=:tt,
                        winning_trades=:wt, losing_trades=:lt,
                        win_rate=:wr, avg_win=:aw, avg_loss=:al,
                        max_drawdown=:md, sharpe_ratio=:sr, profit_factor=:pf
                    WHERE run_id=:rid AND status='RUNNING'
                """),
                {"tp": perf_data.get("total_pnl", 0),
                 "tt": perf_data.get("total_trades", 0),
                 "wt": perf_data.get("winning_trades", 0),
                 "lt": perf_data.get("losing_trades", 0),
                 "wr": perf_data.get("win_rate", 0),
                 "aw": perf_data.get("avg_win", 0),
                 "al": perf_data.get("avg_loss", 0),
                 "md": perf_data.get("max_drawdown", 0),
                 "sr": perf_data.get("sharpe_ratio", 0),
                 "pf": perf_data.get("profit_factor", 0),
                 "rid": run_id}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] update_run_performance 失败 {run_id}: {e}")
        finally:
            self.Session.remove()

    def get_active_runs(self) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM strategy_runs WHERE status='RUNNING' ORDER BY started_at DESC")
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    def get_run_history(self, strategy_name: str, limit: int = 20) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM strategy_runs WHERE strategy_name=:sn ORDER BY started_at DESC LIMIT :lim"),
                {"sn": strategy_name, "lim": limit}
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    def get_all_runs_summary(self) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("""
                    SELECT strategy_name, COUNT(*) as total_runs,
                           SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) as running,
                           SUM(CASE WHEN status='STOPPED' THEN 1 ELSE 0 END) as stopped,
                           MAX(started_at) as last_started
                    FROM strategy_runs
                    GROUP BY strategy_name
                    ORDER BY last_started DESC
                """)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    # ==================== 每日盈亏 ====================

    def save_daily_pnl(self, run_id: str, strategy_name: str,
                        trade_date: str, start_equity: float,
                        end_equity: float, daily_pnl: float,
                        daily_trades: int = 0,
                        high_water_mark: float = 0,
                        daily_drawdown: float = 0,
                        cumulative_pnl: float = 0):
        session = self.Session()
        try:
            session.execute(
                text("""
                    INSERT OR REPLACE INTO strategy_daily_pnl
                    (run_id, strategy_name, trade_date, start_equity, end_equity,
                     daily_pnl, daily_trades, high_water_mark, daily_drawdown, cumulative_pnl)
                    VALUES (:rid, :sn, :td, :se, :ee, :dp, :dt, :hw, :dd, :cp)
                """),
                {"rid": run_id, "sn": strategy_name, "td": trade_date,
                 "se": start_equity, "ee": end_equity, "dp": daily_pnl,
                 "dt": daily_trades, "hw": high_water_mark, "dd": daily_drawdown,
                 "cp": cumulative_pnl}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_daily_pnl 失败 {strategy_name}: {e}")
        finally:
            self.Session.remove()

    def get_daily_pnl(self, strategy_name: str, limit: int = 30) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM strategy_daily_pnl WHERE strategy_name=:sn ORDER BY trade_date DESC LIMIT :lim"),
                {"sn": strategy_name, "lim": limit}
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    # ==================== 绩效快照 ====================

    def save_performance_snapshot(self, strategy_name: str, run_id: str,
                                  perf_data: dict):
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT INTO performance_snapshot
                    (strategy_name, run_id, captured_at, total_pnl, total_trades,
                     winning_trades, losing_trades, win_rate, avg_win, avg_loss,
                     max_drawdown, current_drawdown, sharpe_ratio, profit_factor,
                     open_positions, notes)
                    VALUES (:sn, :rid, :ca, :tp, :tt, :wt, :lt, :wr, :aw, :al,
                            :md, :cd, :sr, :pf, :op, :nt)
                """),
                {"sn": strategy_name, "rid": run_id, "ca": now,
                 "tp": perf_data.get("total_pnl", 0),
                 "tt": perf_data.get("total_trades", 0),
                 "wt": perf_data.get("winning_trades", 0),
                 "lt": perf_data.get("losing_trades", 0),
                 "wr": perf_data.get("win_rate", 0),
                 "aw": perf_data.get("avg_win", 0),
                 "al": perf_data.get("avg_loss", 0),
                 "md": perf_data.get("max_drawdown", 0),
                 "cd": perf_data.get("current_drawdown", 0),
                 "sr": perf_data.get("sharpe_ratio", 0),
                 "pf": perf_data.get("profit_factor", 0),
                 "op": perf_data.get("open_positions", 0),
                 "nt": perf_data.get("notes", "")}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_performance_snapshot 失败 {strategy_name}: {e}")
        finally:
            self.Session.remove()

    def get_latest_performance(self, strategy_name: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT * FROM performance_snapshot WHERE strategy_name=:sn ORDER BY id DESC LIMIT 1"),
                {"sn": strategy_name}
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            self.Session.remove()

    # ==================== strategy_history ====================

    def move_strategy_to_history(self, strategy_name: str, perf_data: dict = None,
                                  removed_by: str = "system", reason: str = "manual") -> bool:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT * FROM strategy_config WHERE strategy_name=:sn"),
                {"sn": strategy_name}
            ).fetchone()
            if not row:
                logger.warning(f"[DB] 策略 {strategy_name} 不在 strategy_config 中，无法移入历史")
                return False
            record = self._row_to_dict(row)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            perf = perf_data or {}
            session.execute(
                text("""
                    INSERT INTO strategy_history
                    (strategy_name, class_name, vt_symbol, market, params, status,
                     start_time, end_time, total_pnl, total_trades, win_rate,
                     removed_by, remove_reason, removed_at, user_id)
                    VALUES (:sn, :cn, :vs, :m, :p, :st, :sta, :ea, :tp, :tt, :wr,
                            :rb, :rr, :ra, :uid)
                """),
                {"sn": record["strategy_name"], "cn": record.get("class_name", ""),
                 "vs": record.get("vt_symbol", ""), "m": record.get("market", ""),
                 "p": record.get("params", "{}"), "st": record.get("status", "REMOVED"),
                 "sta": record.get("created_at", ""), "ea": now,
                 "tp": perf.get("total_pnl", 0), "tt": perf.get("total_trades", 0),
                 "wr": perf.get("win_rate", 0), "rb": removed_by, "rr": reason,
                 "ra": now, "uid": record.get("user_id", "SYSTEM")}
            )
            session.execute(
                text("DELETE FROM strategy_config WHERE strategy_name=:sn"),
                {"sn": strategy_name}
            )
            session.commit()
            logger.info(f"[DB] 📜 {strategy_name} → history (PnL={perf.get('total_pnl',0)}, trades={perf.get('total_trades',0)})")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] move_strategy_to_history 失败 {strategy_name}: {e}")
            return False
        finally:
            self.Session.remove()

    def get_strategy_history(self, market: str = None, limit: int = 50) -> List[dict]:
        session = self.Session()
        try:
            if market:
                rows = session.execute(
                    text("SELECT * FROM strategy_history WHERE market=:m ORDER BY removed_at DESC LIMIT :lim"),
                    {"m": market, "lim": limit}
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM strategy_history ORDER BY removed_at DESC LIMIT :lim"),
                    {"lim": limit}
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    def get_strategy_history_summary(self, days: int = 7) -> dict:
        session = self.Session()
        try:
            row = session.execute(
                text(f"""
                    SELECT COUNT(*),
                           COALESCE(SUM(total_pnl), 0),
                           COALESCE(AVG(win_rate), 0),
                           COALESCE(SUM(total_trades), 0)
                    FROM strategy_history
                    WHERE removed_at > datetime('now', '-{days} days')
                """)
            ).fetchone()
            return {
                "period_days": days,
                "total_removed": row[0] or 0,
                "total_pnl": row[1] or 0,
                "avg_win_rate": row[2] or 0,
                "total_trades": row[3] or 0,
            }
        finally:
            self.Session.remove()

    # ==================== 选股池 ====================

    def add_to_pool(self, symbol: str, score: float, reason: str = "",
                    market: str = "US", source: str = "selector",
                    extra: Optional[dict] = None) -> bool:
        session = self.Session()
        try:
            anomaly_type = "none"
            regime = "range"
            asset_class = "EQUITY"
            extra_json = "{}"
            if extra:
                anomaly_type = extra.get("anomaly_type", "none")
                regime = extra.get("regime", "range")
                asset_class = extra.get("asset_class", "EQUITY")
                remaining = {k: v for k, v in extra.items()
                             if k not in ("anomaly_type", "regime", "asset_class")}
                extra_json = json.dumps(remaining, ensure_ascii=False)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT OR REPLACE INTO ai_stock_pool
                    (symbol, market, score, reason, source, created_at,
                     anomaly_type, regime, asset_class, extra_json)
                    VALUES (:sy, :m, :sc, :re, :so, :ca, :at, :rg, :ac, :ej)
                """),
                {"sy": symbol, "m": market, "sc": score, "re": reason, "so": source,
                 "ca": now, "at": anomaly_type, "rg": regime, "ac": asset_class, "ej": extra_json}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] add_to_pool 失败 {symbol}: {e}")
            return False
        finally:
            self.Session.remove()

    def get_stock_pool(self, market: Optional[str] = None,
                        limit: int = 50) -> List[dict]:
        session = self.Session()
        try:
            if market:
                rows = session.execute(
                    text("SELECT * FROM ai_stock_pool WHERE market=:m ORDER BY score DESC LIMIT :lim"),
                    {"m": market, "lim": limit}
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM ai_stock_pool ORDER BY score DESC LIMIT :lim"),
                    {"lim": limit}
                ).fetchall()
            result = []
            for r in rows:
                d = self._row_to_dict(r)
                if isinstance(d.get("extra_json"), str) and d["extra_json"]:
                    try:
                        d["extra"] = json.loads(d["extra_json"])
                    except Exception:
                        d["extra"] = {}
                else:
                    d["extra"] = {}
                result.append(d)
            return result
        finally:
            self.Session.remove()

    # ==================== save_diagnosis (v3.8.2 增强) ====================

    def save_diagnosis(self, symbol: str, diagnosis, market: str = "US",
                       score: float = 0.0, **kwargs) -> bool:
        session = self.Session()
        try:
            if isinstance(diagnosis, str):
                diagnosis_str = diagnosis
            elif isinstance(diagnosis, dict):
                if "text" in diagnosis:
                    diagnosis_str = str(diagnosis["text"])
                else:
                    diagnosis_str = json.dumps(diagnosis, ensure_ascii=False, default=str)
            elif isinstance(diagnosis, (int, float, bool)):
                diagnosis_str = str(diagnosis)
            else:
                diagnosis_str = str(diagnosis)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT OR REPLACE INTO stock_diagnosis
                    (symbol, market, diagnosis, score, updated_at)
                    VALUES (:sy, :m, :di, :sc, :ua)
                """),
                {"sy": symbol, "m": market, "di": diagnosis_str,
                 "sc": float(score), "ua": now}
            )
            session.commit()
            logger.debug(f"[DB] ✅ 诊断落库: {symbol} market={market}")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_diagnosis 失败 {symbol}: {e} | type={type(diagnosis).__name__}")
            return False
        finally:
            self.Session.remove()

    def get_diagnosis(self, symbol: str, market: str = "US") -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT * FROM stock_diagnosis WHERE symbol=:sy AND market=:m ORDER BY updated_at DESC LIMIT 1"),
                {"sy": symbol, "m": market}
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            self.Session.remove()

    def get_all_diagnoses(self, market: str = None, limit: int = 100) -> List[dict]:
        session = self.Session()
        try:
            if market:
                rows = session.execute(
                    text("SELECT * FROM stock_diagnosis WHERE market=:m ORDER BY updated_at DESC LIMIT :lim"),
                    {"m": market, "lim": limit}
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM stock_diagnosis ORDER BY updated_at DESC LIMIT :lim"),
                    {"lim": limit}
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    def delete_diagnosis(self, symbol: str, market: str = "US") -> bool:
        session = self.Session()
        try:
            session.execute(
                text("DELETE FROM stock_diagnosis WHERE symbol=:sy AND market=:m"),
                {"sy": symbol, "m": market}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] delete_diagnosis 失败 {symbol}: {e}")
            return False
        finally:
            self.Session.remove()

    # ==================== 行情快照 ====================

    def save_quote_snapshot(self, symbol: str, quote: dict, trigger_type: str = "on_start",
                            strategy_name: str = "", **kwargs):
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            extra_json = json.dumps(kwargs, ensure_ascii=False) if kwargs else ""
            session.execute(
                text("""
                    INSERT INTO quote_snapshot
                    (symbol, underlying, timestamp, trigger_type,
                     last_price, open_price, high_price, low_price, prev_close,
                     volume, turnover, implied_volatility, delta, gamma, vega,
                     theta, rho, premium, strike_price, expiry_date_distance,
                     open_interest, recovery_price, price_recovery_ratio,
                     pre_price, after_price, regime, strategy_name, extra_json)
                    VALUES (:sy, :un, :ts, :tt,
                            :lp, :op, :hp, :lp2, :pc,
                            :vo, :to, :iv, :de, :ga, :ve,
                            :th, :rh, :pr, :sp, :ed,
                            :oi, :rp, :prr,
                            :pp, :ap, :rg, :sn, :ej)
                """),
                {"sy": symbol, "un": quote.get("underlying", ""), "ts": now, "tt": trigger_type,
                 "lp": self._f(quote.get("last_price")), "op": self._f(quote.get("open_price")),
                 "hp": self._f(quote.get("high_price")), "lp2": self._f(quote.get("low_price")),
                 "pc": self._f(quote.get("prev_close")), "vo": self._f(quote.get("volume")),
                 "to": self._f(quote.get("turnover")), "iv": self._f(quote.get("implied_volatility")),
                 "de": self._f(quote.get("delta")), "ga": self._f(quote.get("gamma")),
                 "ve": self._f(quote.get("vega")), "th": self._f(quote.get("theta")),
                 "rh": self._f(quote.get("rho")), "pr": self._f(quote.get("premium")),
                 "sp": self._f(quote.get("strike_price")), "ed": self._f(quote.get("expiry_date_distance")),
                 "oi": self._f(quote.get("open_interest")), "rp": self._f(quote.get("recovery_price")),
                 "prr": self._f(quote.get("price_recovery_ratio")), "pp": self._f(quote.get("pre_price")),
                 "ap": self._f(quote.get("after_price")), "rg": quote.get("regime", ""),
                 "sn": strategy_name, "ej": extra_json}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"[DB] save_quote_snapshot 失败 {symbol}: {e}")
        finally:
            self.Session.remove()

    # ==================== 参数历史 ====================

    def save_param_version(self, vt_symbol: str, class_name: str, params: dict,
                           version: int = None, changed_by: str = "system",
                           reason: str = "") -> int:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if version is None:
                row = session.execute(
                    text("SELECT COALESCE(MAX(version), 0) FROM param_history WHERE vt_symbol=:vs AND class_name=:cn"),
                    {"vs": vt_symbol, "cn": class_name}
                ).fetchone()
                version = (row[0] if row else 0) + 1
            session.execute(
                text("""
                    INSERT INTO param_history (vt_symbol, class_name, params, version, created_at, changed_by, reason)
                    VALUES (:vs, :cn, :p, :v, :ca, :cb, :r)
                """),
                {"vs": vt_symbol, "cn": class_name,
                 "p": json.dumps(params, ensure_ascii=False),
                 "v": version, "ca": now, "cb": changed_by, "r": reason}
            )
            session.commit()
            logger.info(f"[DB] 📝 参数版本记录: {vt_symbol}/{class_name} v{version}")
            return version
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_param_version 失败 {vt_symbol}/{class_name}: {e}")
            return 0
        finally:
            self.Session.remove()

    def get_latest_params(self, vt_symbol: str, class_name: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT params FROM param_history WHERE vt_symbol=:vs AND class_name=:cn ORDER BY version DESC LIMIT 1"),
                {"vs": vt_symbol, "cn": class_name}
            ).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except Exception:
                    return None
            return None
        finally:
            self.Session.remove()

    def get_param_version(self, vt_symbol: str, class_name: str, version: int) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT params FROM param_history WHERE vt_symbol=:vs AND class_name=:cn AND version=:v"),
                {"vs": vt_symbol, "cn": class_name, "v": version}
            ).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except Exception:
                    return None
            return None
        finally:
            self.Session.remove()

    def get_param_history(self, vt_symbol: str, class_name: str, limit: int = 20) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM param_history WHERE vt_symbol=:vs AND class_name=:cn ORDER BY version DESC LIMIT :lim"),
                {"vs": vt_symbol, "cn": class_name, "lim": limit}
            ).fetchall()
            result = []
            for r in rows:
                d = self._row_to_dict(r)
                if isinstance(d.get("params"), str):
                    try:
                        d["params"] = json.loads(d["params"])
                    except Exception:
                        d["params"] = {}
                result.append(d)
            return result
        finally:
            self.Session.remove()

    def archive_strategy_params(self, strategy_name: str, vt_symbol: str,
                                 class_name: str, old_params: dict,
                                 new_params: dict = None,
                                 changed_by: str = "system",
                                 reason: str = "") -> bool:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT INTO strategy_params_archive
                    (strategy_name, vt_symbol, class_name, old_params, new_params,
                     changed_by, reason, archived_at)
                    VALUES (:sn, :vs, :cn, :op, :np, :cb, :r, :at)
                """),
                {"sn": strategy_name, "vs": vt_symbol, "cn": class_name,
                 "op": json.dumps(old_params, ensure_ascii=False),
                 "np": json.dumps(new_params or {}, ensure_ascii=False),
                 "cb": changed_by, "r": reason, "at": now}
            )
            session.commit()
            logger.info(f"[DB] 📝 {strategy_name} 参数已归档 (reason={reason})")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] archive_strategy_params 失败 {strategy_name}: {e}")
            return False
        finally:
            self.Session.remove()

    # ==================== 部署日志 & 事件 ====================

    def log_deploy(self, strategy_name: str, version: int, action: str,
                   operator: str, result: str, message: str):
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT INTO deploy_log (strategy_name, version, action, operator, result, message, created_at)
                    VALUES (:sn, :v, :act, :op, :res, :msg, :ca)
                """),
                {"sn": strategy_name, "v": version, "act": action, "op": operator,
                 "res": result, "msg": message, "ca": now}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] log_deploy 失败 {strategy_name}: {e}")
        finally:
            self.Session.remove()

    def log_event(self, timestamp: str, level: str, message: str,
                  strategy_name: str = "", user_id: str = "SYSTEM"):
        session = self.Session()
        try:
            session.execute(
                text("""
                    INSERT INTO events (timestamp, level, message, strategy_name, user_id)
                    VALUES (:ts, :lv, :msg, :sn, :uid)
                """),
                {"ts": timestamp, "lv": level, "msg": message,
                 "sn": strategy_name, "uid": user_id}
            )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] log_event 失败: {e}")
        finally:
            self.Session.remove()

    def get_events(self, limit: int = 100, level: str = "") -> List[dict]:
        session = self.Session()
        try:
            if level:
                rows = session.execute(
                    text("SELECT * FROM events WHERE level=:l ORDER BY id DESC LIMIT :lim"),
                    {"l": level, "lim": limit}
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM events ORDER BY id DESC LIMIT :lim"),
                    {"lim": limit}
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    # ==================== Regime 查询 ====================

    def get_latest_regime(self, vt_symbol: str, market: str = "US") -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("""
                    SELECT regime, prob_trend, prob_range, prob_volatile,
                           confidence, features_json, timestamp
                    FROM regime_records
                    WHERE symbol = :sy AND exchange = :ex
                    ORDER BY timestamp DESC LIMIT 1
                """),
                {"sy": vt_symbol, "ex": market}
            ).fetchone()
            if not row:
                return None
            return {
                "regime": row[0] or "range",
                "prob_trend": row[1] or 0,
                "prob_range": row[2] or 0,
                "prob_volatile": row[3] or 0,
                "confidence": row[4] or 0.5,
                "features": row[5] or "{}",
                "timestamp": row[6] or "",
            }
        finally:
            self.Session.remove()

    def save_regime(self, symbol: str, exchange: str, regime: str,
                     prob_trend: float = 0, prob_range: float = 0,
                     prob_volatile: float = 0, confidence: float = 0.5,
                     features: dict = None) -> bool:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            features_json = json.dumps(features or {}, ensure_ascii=False)
            session.execute(
                text("""
                    INSERT OR REPLACE INTO regime_records
                    (symbol, exchange, regime, prob_trend, prob_range, prob_volatile,
                     confidence, features_json, timestamp)
                    VALUES (:sy, :ex, :rg, :pt, :pr, :pv, :cf, :fj, :ts)
                """),
                {"sy": symbol, "ex": exchange, "rg": regime,
                 "pt": prob_trend, "pr": prob_range, "pv": prob_volatile,
                 "cf": confidence, "fj": features_json, "ts": now}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_regime 失败 {symbol}: {e}")
            return False
        finally:
            self.Session.remove()

    # ==================== 启动恢复 ====================

    def get_running_strategies_for_restart(self) -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("""
                    SELECT r.strategy_name, r.run_id, r.class_name, r.vt_symbol,
                           r.market, r.params_json, sc.params as sc_params
                    FROM strategy_runs r
                    LEFT JOIN strategy_config sc ON sc.strategy_name = r.strategy_name
                    WHERE r.status = 'RUNNING'
                    ORDER BY r.started_at DESC
                """)
            ).fetchall()
            result = []
            for r in rows:
                d = self._row_to_dict(r)
                if d.get("sc_params"):
                    try:
                        d["params"] = json.loads(d["sc_params"])
                    except Exception:
                        d["params"] = {}
                elif d.get("params_json"):
                    try:
                        d["params"] = json.loads(d["params_json"])
                    except Exception:
                        d["params"] = {}
                else:
                    d["params"] = {}
                result.append(d)
            return result
        finally:
            self.Session.remove()

    # 兼容别名
    def get_running_strategies(self) -> List[dict]:
        return self.get_running_strategies_for_restart()

    # ==================== 策略绩效查询 ====================

    def get_strategy_performance(self, strategy_name: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("""
                    SELECT total_pnl, total_trades, winning_trades, losing_trades,
                           win_rate, avg_win, avg_loss, max_drawdown, sharpe_ratio, profit_factor
                    FROM performance_snapshot
                    WHERE strategy_name=:sn
                    ORDER BY id DESC LIMIT 1
                """),
                {"sn": strategy_name}
            ).fetchone()
            if row:
                columns = ['total_pnl', 'total_trades', 'winning_trades', 'losing_trades',
                           'win_rate', 'avg_win', 'avg_loss', 'max_drawdown', 'sharpe_ratio', 'profit_factor']
                return dict(zip(columns, row))
            row = session.execute(
                text("""
                    SELECT total_pln, total_trades, winning_trades, losing_trades,
                           win_rate, avg_win, avg_loss, max_drawdown, sharpe_ratio
                    FROM strategy_runs
                    WHERE strategy_name=:sn
                    ORDER BY started_at DESC LIMIT 1
                """),
                {"sn": strategy_name}
            ).fetchone()
            if not row:
                return None
            columns = ['total_pnl', 'total_trades', 'winning_trades', 'losing_trades',
                       'win_rate', 'avg_win', 'avg_loss', 'max_drawdown', 'sharpe_ratio']
            return dict(zip(columns, row))
        finally:
            self.Session.remove()

    def get_strategy_params_summary(self, strategy_name: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT params FROM strategy_config WHERE strategy_name=:sn"),
                {"sn": strategy_name}
            ).fetchone()
            if not row:
                return None
            try:
                params = json.loads(row[0])
            except Exception:
                params = {}
            keys = list(params.keys())[:8]
            return {k: params[k] for k in keys}
        finally:
            self.Session.remove()

    # ==================== 用户权益跟踪 ====================

    def save_user_equity(self, user_id: str, equity: float, cash: float = 0,
                          market_val: float = 0, frozen: float = 0,
                          power: float = 0, currency: str = "USD") -> bool:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT INTO user_equity
                    (user_id, equity, cash, market_val, frozen, power, currency, recorded_at)
                    VALUES (:uid, :eq, :ca, :mv, :fr, :pw, :cu, :ra)
                """),
                {"uid": user_id, "eq": equity, "ca": cash, "mv": market_val,
                 "fr": frozen, "pw": power, "cu": currency, "ra": now}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_user_equity 失败 {user_id}: {e}")
            return False
        finally:
            self.Session.remove()

    def get_latest_user_equity(self, user_id: str) -> Optional[dict]:
        session = self.Session()
        try:
            row = session.execute(
                text("SELECT * FROM user_equity WHERE user_id=:uid ORDER BY recorded_at DESC LIMIT 1"),
                {"uid": user_id}
            ).fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            self.Session.remove()

    # ==================== Positions 持仓管理 ====================

    def upsert_position(self, user_id: str, symbol: str, market: str,
                        quantity: float, avg_cost: float = 0,
                        last_price: float = 0, pnl: float = 0,
                        is_managed: bool = False) -> bool:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
            session.execute(
                text("""
                    INSERT INTO positions
                        (user_id, symbol, market, quantity, avg_cost, last_price, pnl, is_managed, updated_at)
                    VALUES
                        (:uid, :sym, :mkt, :qty, :ac, :lp, :pnl, :im, :ua)
                    ON CONFLICT(user_id, symbol)
                    DO UPDATE SET
                        market = :mkt,
                        quantity = :qty,
                        avg_cost = :ac,
                        last_price = :lp,
                        pnl = :pnl,
                        is_managed = :im,
                        updated_at = :ua
                """),
                {"uid": user_id, "sym": symbol, "mkt": market,
                 "qty": quantity, "ac": avg_cost, "lp": last_price,
                 "pnl": pnl, "im": 1 if is_managed else 0, "ua": now}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] upsert_position 失败 {symbol}: {e}")
            return False
        finally:
            self.Session.remove()

    def get_positions(self, user_id: str = "SYSTEM") -> List[dict]:
        session = self.Session()
        try:
            rows = session.execute(
                text("SELECT * FROM positions WHERE user_id=:uid ORDER BY symbol"),
                {"uid": user_id}
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    def delete_position(self, user_id: str, symbol: str) -> bool:
        session = self.Session()
        try:
            session.execute(
                text("DELETE FROM positions WHERE user_id=:uid AND symbol=:sym"),
                {"uid": user_id, "sym": symbol}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] delete_position 失败 {symbol}: {e}")
            return False
        finally:
            self.Session.remove()

    # ==================== dbbardata 管理 ====================

    def insert_dbbardata(self, bars: List[dict]):
        if not bars:
            return 0
        session = self.Session()
        try:
            count = 0
            for bar in bars:
                session.execute(
                    text("""
                        INSERT INTO dbbardata
                        (datetime, symbol, open, high, low, close, volume, turnover)
                        VALUES (:dt, :s, :o, :h, :l, :c, :v, :to)
                    """),
                    {"dt": bar.get("datetime", ""),
                     "s": bar.get("symbol", ""),
                     "o": self._f(bar.get("open")),
                     "h": self._f(bar.get("high")),
                     "l": self._f(bar.get("low")),
                     "c": self._f(bar.get("close")),
                     "v": self._f(bar.get("volume")),
                     "to": self._f(bar.get("turnover"))}
                )
                count += 1
            session.commit()
            return count
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] insert_dbbardata 失败: {e}")
            return 0
        finally:
            self.Session.remove()

    def get_dbbardata_count(self, hours: int = 1) -> int:
        session = self.Session()
        try:
            row = session.execute(
                text(f"SELECT COUNT(*) FROM dbbardata WHERE datetime > datetime('now', '-{hours} hours')")
            ).fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"[DB] get_dbbardata_count 失败: {e}")
            return 0
        finally:
            self.Session.remove()

    # ==================== 订单信号记录 ====================

    def save_order_signal(self, signal_id: str, user_id: str, symbol: str,
                           direction: str, price: float, volume: int,
                           offset: str = "OPEN", strategy_name: str = "",
                           status: str = "PENDING") -> bool:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT OR REPLACE INTO order_signals
                    (signal_id, user_id, symbol, direction, price, volume,
                     offset, strategy_name, status, submitted_at)
                    VALUES (:sid, :uid, :sy, :dr, :pr, :vo, :of, :sn, :st, :sa)
                """),
                {"sid": signal_id, "uid": user_id, "sy": symbol, "dr": direction,
                 "pr": price, "vo": volume, "of": offset,
                 "sn": strategy_name, "st": status, "sa": now}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] save_order_signal 失败 {signal_id}: {e}")
            return False
        finally:
            self.Session.remove()

    def update_order_signal_fill(self, signal_id: str, fill_price: float,
                                  fill_volume: int, commission: float = 0.0,
                                  pnl: float = 0.0) -> bool:
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    UPDATE order_signals SET
                        filled_at=:fa, fill_price=:fp, fill_volume=:fv,
                        commission=:co, pnl=:pnl
                    WHERE signal_id=:sid
                """),
                {"fa": now, "fp": fill_price, "fv": fill_volume,
                 "co": commission, "pnl": pnl, "sid": signal_id}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] update_order_signal_fill 失败 {signal_id}: {e}")
            return False
        finally:
            self.Session.remove()

    def get_order_signals(self, user_id: str = "", limit: int = 100) -> List[dict]:
        """
        查询订单信号列表（scheduler_jobs._diagnose_no_orders 调用）
        返回完整记录列表。
        """
        session = self.Session()
        try:
            if user_id:
                rows = session.execute(
                    text("SELECT * FROM order_signals WHERE user_id=:uid ORDER BY submitted_at DESC LIMIT :lim"),
                    {"uid": user_id, "lim": limit}
                ).fetchall()
            else:
                rows = session.execute(
                    text("SELECT * FROM order_signals ORDER BY submitted_at DESC LIMIT :lim"),
                    {"lim": limit}
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    def get_order_signals_recent(self, minutes: int = 30,
                                  user_id: str = "") -> List[dict]:
        """
        查询最近 N 分钟的订单信号（用于不下单诊断）
        scheduler_jobs._diagnose_no_orders 直接调用此方法。
        """
        session = self.Session()
        try:
            if user_id:
                rows = session.execute(
                    text("""
                        SELECT * FROM order_signals
                        WHERE user_id=:uid
                          AND submitted_at > datetime('now', :offset)
                        ORDER BY submitted_at DESC
                    """),
                    {"uid": user_id, "offset": f"-{minutes} minutes"}
                ).fetchall()
            else:
                rows = session.execute(
                    text("""
                        SELECT * FROM order_signals
                        WHERE submitted_at > datetime('now', :offset)
                        ORDER BY submitted_at DESC
                    """),
                    {"offset": f"-{minutes} minutes"}
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()

    # ==================== trade_log CRUD（供 lifecycle 衰减检测）====================

    def insert_trade(self, strategy_name: str, run_id: str, symbol: str,
                      direction: str, entry_price: float, quantity: float = 0,
                      entry_time: str = "") -> int:
        """记录一笔新开仓交易，返回 trade_id"""
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT INTO trade_log
                    (strategy_name, run_id, symbol, direction,
                     entry_price, quantity, entry_time, status, created_at)
                    VALUES (:sn, :rid, :sy, :dr, :ep, :qty, :et, 'OPEN', :ca)
                """),
                {"sn": strategy_name, "rid": run_id, "sy": symbol,
                 "dr": direction, "ep": entry_price, "qty": quantity,
                 "et": entry_time or now, "ca": now}
            )
            session.commit()
            return session.execute(text("SELECT last_insert_rowid()")).fetchone()[0]
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] insert_trade 失败: {e}")
            return 0
        finally:
            self.Session.remove()

    def close_trade(self, trade_id: int, exit_price: float, pnl: float = 0,
                      reason: str = "", close_time: str = "") -> bool:
        """平仓一笔交易"""
        session = self.Session()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    UPDATE trade_log SET
                        exit_price=:xp, pnl=:pnl, status='CLOSED',
                        reason=:r, close_time=:ct
                    WHERE id=:tid
                """),
                {"xp": exit_price, "pnl": pnl, "r": reason,
                 "ct": close_time or now, "tid": trade_id}
            )
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[DB] close_trade 失败: {e}")
            return False
        finally:
            self.Session.remove()

    def get_recent_trades(self, strategy_name: str, limit: int = 20,
                           status: str = "CLOSED") -> List[dict]:
        """获取策略最近 N 笔交易（用于衰减检测聚合）"""
        session = self.Session()
        try:
            rows = session.execute(
                text("""
                    SELECT * FROM trade_log
                    WHERE strategy_name=:sn AND status=:st
                    ORDER BY COALESCE(close_time, created_at) DESC
                    LIMIT :lim
                """),
                {"sn": strategy_name, "st": status, "lim": limit}
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self.Session.remove()
