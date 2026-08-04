"""
core/db_manager.py - Apollo Trader v3.3.0 全功能数据库管理器
变更：
  v3.3.0 - 新增 strategy_history 表（已删除策略的历史记录，含绩效）
           新增 move_strategy_to_history() 方法
           新增 get_strategy_history() 查询方法
           _create_tables 中 strategy_config 增加 created_at/updated_at 列迁移
"""
import sqlite3
import json
import logging
import uuid
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple

logger = logging.getLogger("DBManager")


class DBManager:
    """数据库管理器 v3.3.0"""

    def __init__(self, db_path: str = "data/history.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self._create_tables()
        self._migrate_all_tables()

    # ==================== 事务辅助 ====================

    def _safe_commit(self):
        try:
            self.conn.commit()
        except sqlite3.OperationalError as e:
            if "no transaction" in str(e):
                pass
            else:
                raise

    # ==================== 建表 ====================

    def _create_tables(self):
        cursor = self.conn.cursor()

        # === strategy_config ===
        cursor.execute("""
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
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)

        # === tick_data ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tick_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, exchange TEXT, datetime TEXT, gateway_name TEXT,
                name TEXT, last_price REAL, volume REAL, turnover REAL,
                open_price REAL, high_price REAL, low_price REAL, pre_close REAL,
                bid_price_1 REAL, ask_price_1 REAL, bid_volume_1 REAL, ask_volume_1 REAL,
                source TEXT, saved_at_utc TEXT, received_at TEXT
            )
        """)

        # === quote_snapshot ===
        cursor.execute("""
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
        """)

        # === ai_stock_pool ===
        cursor.execute("""
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
        """)

        # === stock_diagnosis ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_diagnosis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, market TEXT DEFAULT 'US',
                diagnosis TEXT DEFAULT '', score REAL DEFAULT 0,
                updated_at TEXT DEFAULT ''
            )
        """)

        # === deploy_log ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deploy_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT, version INTEGER, action TEXT,
                operator TEXT, result TEXT, message TEXT, created_at TEXT DEFAULT ''
            )
        """)

        # === events ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, level TEXT, message TEXT, strategy_name TEXT DEFAULT ''
            )
        """)

        # === param_history ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS param_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT, class_name TEXT, params TEXT,
                version INTEGER, created_at TEXT DEFAULT ''
            )
        """)

        # === regime_records ===
        cursor.execute("""
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
        """)

        # === strategy_runs ===
        cursor.execute("""
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
                exit_reason TEXT DEFAULT '',
                notes TEXT DEFAULT ''
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_name ON strategy_runs(strategy_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON strategy_runs(status)")

        # === strategy_daily_pnl ===
        cursor.execute("""
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
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_name ON strategy_daily_pnl(strategy_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_run ON strategy_daily_pnl(run_id)")

        # === performance_snapshot ===
        cursor.execute("""
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
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_perf_name ON performance_snapshot(strategy_name)")

        # === strategy_history (v3.3.0 新增) ===
        cursor.execute("""
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
                removed_at TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hist_name ON strategy_history(strategy_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hist_market ON strategy_history(market)")

        self._safe_commit()
        logger.info(f"[DB INIT] ✅ 数据库表创建/验证完成 ({self.db_path})")

    def _migrate_all_tables(self):
        """自动补全缺失列"""
        sc_additions = {
            'status': "ALTER TABLE strategy_config ADD COLUMN status TEXT DEFAULT 'PENDING'",
            'status_msg': "ALTER TABLE strategy_config ADD COLUMN status_msg TEXT DEFAULT ''",
            'enabled': "ALTER TABLE strategy_config ADD COLUMN enabled INTEGER DEFAULT 1",
            'active': "ALTER TABLE strategy_config ADD COLUMN active INTEGER DEFAULT 1",
            'current_version': "ALTER TABLE strategy_config ADD COLUMN current_version INTEGER DEFAULT 1",
            'version': "ALTER TABLE strategy_config ADD COLUMN version INTEGER DEFAULT 1",
            'created_at': "ALTER TABLE strategy_config ADD COLUMN created_at TEXT DEFAULT ''",
            'updated_at': "ALTER TABLE strategy_config ADD COLUMN updated_at TEXT DEFAULT ''",
        }
        self._migrate_columns("strategy_config", sc_additions)

        asp_additions = {
            'anomaly_type': "ALTER TABLE ai_stock_pool ADD COLUMN anomaly_type TEXT DEFAULT 'none'",
            'regime': "ALTER TABLE ai_stock_pool ADD COLUMN regime TEXT DEFAULT 'range'",
            'asset_class': "ALTER TABLE ai_stock_pool ADD COLUMN asset_class TEXT DEFAULT 'EQUITY'",
            'extra_json': "ALTER TABLE ai_stock_pool ADD COLUMN extra_json TEXT DEFAULT '{}'",
        }
        self._migrate_columns("ai_stock_pool", asp_additions)

        rr_additions = {
            'exchange': "ALTER TABLE regime_records ADD COLUMN exchange TEXT NOT NULL DEFAULT 'US'",
            'features_json': "ALTER TABLE regime_records ADD COLUMN features_json TEXT DEFAULT '{}'",
        }
        self._migrate_columns("regime_records", rr_additions)

    def _migrate_columns(self, table: str, additions: Dict[str, str]):
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        for col, ddl in additions.items():
            if col not in existing:
                try:
                    cursor.execute(ddl)
                    self._safe_commit()
                    logger.info(f"[DB] 迁移: {table} 添加 {col} 列")
                except Exception as e:
                    logger.debug(f"[DB] 迁移 {table}.{col} 失败: {e}")

    # ========== 策略 CRUD ==========

    def get_all_strategies(self, enabled_only: bool = False) -> List[dict]:
        cursor = self.conn.cursor()
        if enabled_only:
            cursor.execute("SELECT * FROM strategy_config WHERE enabled = 1 ORDER BY updated_at DESC")
        else:
            cursor.execute("SELECT * FROM strategy_config ORDER BY updated_at DESC")
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if isinstance(d.get("params"), str):
                try:
                    d["params"] = json.loads(d["params"])
                except Exception:
                    d["params"] = {}
            result.append(d)
        return result

    def get_strategy(self, strategy_name: str) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM strategy_config WHERE strategy_name = ?", (strategy_name,))
        row = cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("params"), str):
            try:
                d["params"] = json.loads(d["params"])
            except Exception:
                d["params"] = {}
        return d

    def save_strategy(self, strategy_name: str, class_name: str,
                      vt_symbol: str, market: str,
                      params: dict, source: str = "", modifier: str = "") -> tuple:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params_json = json.dumps(params, ensure_ascii=False)
        existing = self.get_strategy(strategy_name)
        cursor = self.conn.cursor()
        if existing:
            new_version = existing.get("current_version", 1) + 1
            cursor.execute("""
                UPDATE strategy_config SET
                    class_name=?, vt_symbol=?, market=?, params=?,
                    current_version=?, version=?, source=?, modifier=?,
                    updated_at=?, status='PENDING', enabled=1, active=1
                WHERE strategy_name=?
            """, (class_name, vt_symbol, market, params_json,
                  new_version, new_version, source, modifier,
                  now, strategy_name))
            self._safe_commit()
            self._save_param_history(vt_symbol, class_name, params, new_version)
            return True, new_version
        else:
            cursor.execute("""
                INSERT INTO strategy_config
                (strategy_name, class_name, vt_symbol, market, params,
                 enabled, active, version, current_version, status,
                 source, modifier, created_at, updated_at)
                VALUES (?,?,?,?,?, 1,1,1,1,'PENDING', ?,?,?,?)
            """, (strategy_name, class_name, vt_symbol, market, params_json,
                  source, modifier, now, now))
            self._safe_commit()
            self._save_param_history(vt_symbol, class_name, params, 1)
            return True, 1

    def disable_strategy(self, strategy_name: str):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE strategy_config SET enabled=0, active=0, status='DISABLED' WHERE strategy_name=?", (strategy_name,))
        self._safe_commit()

    def mark_deployed(self, strategy_name: str, version: int, operator: str = "system"):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        cursor.execute("UPDATE strategy_config SET status='RUNNING', updated_at=? WHERE strategy_name=?", (now, strategy_name))
        self._safe_commit()

    # ========== 状态追踪 ==========

    def set_strategy_status(self, strategy_name: str, status: str, msg: str = ""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        cursor.execute("UPDATE strategy_config SET status=?, status_msg=?, updated_at=? WHERE strategy_name=?",
                       (status, msg, now, strategy_name))
        self._safe_commit()
        logger.info(f"[DB] 🏷️ {strategy_name} status → {status}")

    def get_strategy_status(self, strategy_name: str) -> Optional[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT status FROM strategy_config WHERE strategy_name=?", (strategy_name,))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_status_summary(self) -> dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as cnt FROM strategy_config GROUP BY status")
        rows = cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    def get_active_strategies(self) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM strategy_config WHERE enabled=1 AND active=1 ORDER BY updated_at DESC")
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if isinstance(d.get("params"), str):
                try:
                    d["params"] = json.loads(d["params"])
                except Exception:
                    d["params"] = {}
            result.append(d)
        return result

    # ========== ★ 运行生命周期管理 ==========

    def start_run(self, strategy_name: str, class_name: str,
                  vt_symbol: str, market: str,
                  params: dict) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params_json = json.dumps(params, ensure_ascii=False)
        params_hash = hashlib.md5(params_json.encode()).hexdigest() if isinstance(params, dict) else ""

        self._end_orphan_runs(strategy_name, reason="restart")

        run_id = str(uuid.uuid4())[:12]
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO strategy_runs
            (run_id, strategy_name, class_name, vt_symbol, market,
             params_hash, params_json, started_at, status)
            VALUES (?,?,?,?,?, ?,?,?, 'RUNNING')
        """, (run_id, strategy_name, class_name, vt_symbol, market,
              params_hash, params_json, now))
        self._safe_commit()
        logger.info(f"[DB] 🏃 {strategy_name} 运行开始 run_id={run_id}")
        return run_id

    def _end_orphan_runs(self, strategy_name: str, reason: str = "restart"):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE strategy_runs SET ended_at=?, status='STOPPED', exit_reason=?
            WHERE strategy_name=? AND status='RUNNING'
        """, (now, reason, strategy_name))
        if cursor.rowcount > 0:
            self._safe_commit()
            logger.info(f"[DB] 🔚 {strategy_name} 关闭 {cursor.rowcount} 个旧运行记录 (reason={reason})")

    def end_run(self, run_id: str, exit_reason: str = "manual",
                perf_data: Optional[dict] = None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        if perf_data:
            cursor.execute("""
                UPDATE strategy_runs SET
                    ended_at=?, status='STOPPED',
                    total_pnl=?, total_trades=?,
                    winning_trades=?, losing_trades=?,
                    win_rate=?, avg_win=?, avg_loss=?,
                    max_drawdown=?, sharpe_ratio=?,
                    exit_reason=?
                WHERE run_id=?
            """, (now, perf_data.get("total_pnl", 0),
                  perf_data.get("total_trades", 0),
                  perf_data.get("winning_trades", 0),
                  perf_data.get("losing_trades", 0),
                  perf_data.get("win_rate", 0),
                  perf_data.get("avg_win", 0),
                  perf_data.get("avg_loss", 0),
                  perf_data.get("max_drawdown", 0),
                  perf_data.get("sharpe_ratio", 0),
                  exit_reason, run_id))
        else:
            cursor.execute("""
                UPDATE strategy_runs SET ended_at=?, status='STOPPED', exit_reason=?
                WHERE run_id=?
            """, (now, exit_reason, run_id))
        self._safe_commit()
        logger.info(f"[DB] 🔚 run_id={run_id} 已结束 (reason={exit_reason})")

    def update_run_performance(self, run_id: str, perf_data: dict):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE strategy_runs SET
                total_pnl=?, total_trades=?,
                winning_trades=?, losing_trades=?,
                win_rate=?, avg_win=?, avg_loss=?,
                max_drawdown=?, sharpe_ratio=?
            WHERE run_id=? AND status='RUNNING'
        """, (perf_data.get("total_pnl", 0),
              perf_data.get("total_trades", 0),
              perf_data.get("winning_trades", 0),
              perf_data.get("losing_trades", 0),
              perf_data.get("win_rate", 0),
              perf_data.get("avg_win", 0),
              perf_data.get("avg_loss", 0),
              perf_data.get("max_drawdown", 0),
              perf_data.get("sharpe_ratio", 0),
              run_id))
        self._safe_commit()

    def get_active_runs(self) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM strategy_runs WHERE status='RUNNING' ORDER BY started_at DESC")
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_run_history(self, strategy_name: str, limit: int = 20) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM strategy_runs WHERE strategy_name=?
            ORDER BY started_at DESC LIMIT ?
        """, (strategy_name, limit))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_all_runs_summary(self) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT strategy_name, COUNT(*) as total_runs,
                   SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) as running,
                   SUM(CASE WHEN status='STOPPED' THEN 1 ELSE 0 END) as stopped,
                   MAX(started_at) as last_started
            FROM strategy_runs
            GROUP BY strategy_name
            ORDER BY last_started DESC
        """)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # ========== ★ 每日盈亏 ==========

    def save_daily_pnl(self, run_id: str, strategy_name: str,
                        trade_date: str, start_equity: float,
                        end_equity: float, daily_pnl: float,
                        daily_trades: int = 0,
                        high_water_mark: float = 0,
                        daily_drawdown: float = 0,
                        cumulative_pnl: float = 0):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO strategy_daily_pnl
            (run_id, strategy_name, trade_date, start_equity, end_equity,
             daily_pnl, daily_trades, high_water_mark, daily_drawdown, cumulative_pnl)
            VALUES (?,?,?,?,?, ?,?,?,?,?)
        """, (run_id, strategy_name, trade_date, start_equity, end_equity,
              daily_pnl, daily_trades, high_water_mark, daily_drawdown, cumulative_pnl))
        self._safe_commit()

    def get_daily_pnl(self, strategy_name: str, limit: int = 30) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM strategy_daily_pnl WHERE strategy_name=?
            ORDER BY trade_date DESC LIMIT ?
        """, (strategy_name, limit))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # ========== ★ 绩效快照 ==========

    def save_performance_snapshot(self, strategy_name: str, run_id: str,
                                  perf_data: dict):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO performance_snapshot
            (strategy_name, run_id, captured_at, total_pnl, total_trades,
             winning_trades, losing_trades, win_rate, avg_win, avg_loss,
             max_drawdown, current_drawdown, sharpe_ratio, profit_factor,
             open_positions, notes)
            VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?)
        """, (strategy_name, run_id, now,
              perf_data.get("total_pnl", 0),
              perf_data.get("total_trades", 0),
              perf_data.get("winning_trades", 0),
              perf_data.get("losing_trades", 0),
              perf_data.get("win_rate", 0),
              perf_data.get("avg_win", 0),
              perf_data.get("avg_loss", 0),
              perf_data.get("max_drawdown", 0),
              perf_data.get("current_drawdown", 0),
              perf_data.get("sharpe_ratio", 0),
              perf_data.get("profit_factor", 0),
              perf_data.get("open_positions", 0),
              perf_data.get("notes", "")))
        self._safe_commit()

    def get_latest_performance(self, strategy_name: str) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM performance_snapshot WHERE strategy_name=?
            ORDER BY id DESC LIMIT 1
        """, (strategy_name,))
        row = cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))

    # ========== ★ strategy_history（v3.3.0 核心） ==========

    def move_strategy_to_history(self, strategy_name: str, perf_data: dict = None,
                                  removed_by: str = "system", reason: str = "manual") -> bool:
        """
        将策略从 strategy_config 移到 strategy_history。
        记录运行起止时间 + 最终绩效，供用户查询。
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM strategy_config WHERE strategy_name = ?", (strategy_name,))
        row = cursor.fetchone()
        if not row:
            logger.warning(f"[DB] 策略 {strategy_name} 不在 strategy_config 中，无法移入历史")
            return False
        columns = [desc[0] for desc in cursor.description]
        record = dict(zip(columns, row))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        perf = perf_data or {}

        cursor.execute("""
            INSERT INTO strategy_history
            (strategy_name, class_name, vt_symbol, market, params, status,
             start_time, end_time, total_pnl, total_trades, win_rate,
             removed_by, remove_reason, removed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["strategy_name"],
            record.get("class_name", ""),
            record.get("vt_symbol", ""),
            record.get("market", ""),
            record.get("params", "{}"),
            record.get("status", "REMOVED"),
            record.get("created_at", ""),   # 首次创建时间 = 运行起始
            now,                            # 删除时间 = 运行结束
            perf.get("total_pnl", 0),
            perf.get("total_trades", 0),
            perf.get("win_rate", 0),
            removed_by,
            reason,
            now,
        ))

        # 物理删除 strategy_config 中的记录
        cursor.execute("DELETE FROM strategy_config WHERE strategy_name = ?", (strategy_name,))
        self._safe_commit()
        logger.info(f"[DB] 📜 {strategy_name} → history (PnL={perf.get('total_pnl',0)}, trades={perf.get('total_trades',0)})")
        return True

    def get_strategy_history(self, market: str = None, limit: int = 50) -> List[dict]:
        """查询历史策略记录，供用户查询"""
        cursor = self.conn.cursor()
        if market:
            cursor.execute("""
                SELECT * FROM strategy_history
                WHERE market = ?
                ORDER BY removed_at DESC LIMIT ?
            """, (market, limit))
        else:
            cursor.execute("""
                SELECT * FROM strategy_history
                ORDER BY removed_at DESC LIMIT ?
            """, (limit,))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # ========== 选股池 ==========

    def add_to_pool(self, symbol: str, score: float, reason: str = "",
                    market: str = "US", source: str = "selector",
                    extra: Optional[dict] = None) -> bool:
        try:
            cursor = self.conn.cursor()
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
            cursor.execute("""
                INSERT OR REPLACE INTO ai_stock_pool
                (symbol, market, score, reason, source, created_at,
                 anomaly_type, regime, asset_class, extra_json)
                VALUES (?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?)
            """, (symbol, market, score, reason, source, now,
                   anomaly_type, regime, asset_class, extra_json))
            self._safe_commit()
            return True
        except Exception as e:
            logger.error(f"[DB] add_to_pool 失败 {symbol}: {e}")
            return False

    def get_stock_pool(self, market: Optional[str] = None,
                        limit: int = 50) -> List[dict]:
        cursor = self.conn.cursor()
        if market:
            cursor.execute("SELECT * FROM ai_stock_pool WHERE market=? ORDER BY score DESC LIMIT ?", (market, limit))
        else:
            cursor.execute("SELECT * FROM ai_stock_pool ORDER BY score DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if isinstance(d.get("extra_json"), str) and d["extra_json"]:
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            else:
                d["extra"] = {}
            result.append(d)
        return result

    def save_diagnosis(self, symbol: str, diagnosis: Any, market: str = "US",
                       score: float = 0.0, **kwargs):
        try:
            if isinstance(diagnosis, dict):
                diagnosis_str = diagnosis.get("text", "") or json.dumps(diagnosis, ensure_ascii=False)
            elif not isinstance(diagnosis, str):
                diagnosis_str = str(diagnosis)
            else:
                diagnosis_str = diagnosis
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO stock_diagnosis
                (symbol, market, diagnosis, score, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (symbol, market, diagnosis_str, score,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self._safe_commit()
        except Exception as e:
            logger.warning(f"[DB] save_diagnosis 失败 {symbol}: {e}")

    # ========== 行情快照 ==========

    def save_quote_snapshot(self, symbol: str, quote: dict, trigger_type: str = "on_start",
                            strategy_name: str = "", **kwargs):
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO quote_snapshot
                (symbol, underlying, timestamp, trigger_type,
                 last_price, open_price, high_price, low_price, prev_close,
                 volume, turnover, implied_volatility, delta, gamma, vega,
                 theta, rho, premium, strike_price, expiry_date_distance,
                 open_interest, recovery_price, price_recovery_ratio,
                 pre_price, after_price, regime, strategy_name, extra_json)
                VALUES (?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?)
            """, (
                symbol, quote.get("underlying", ""), now, trigger_type,
                self._f(quote.get("last_price")), self._f(quote.get("open_price")),
                self._f(quote.get("high_price")), self._f(quote.get("low_price")),
                self._f(quote.get("prev_close")), self._f(quote.get("volume")),
                self._f(quote.get("turnover")), self._f(quote.get("implied_volatility")),
                self._f(quote.get("delta")), self._f(quote.get("gamma")),
                self._f(quote.get("vega")), self._f(quote.get("theta")),
                self._f(quote.get("rho")), self._f(quote.get("premium")),
                self._f(quote.get("strike_price")), self._f(quote.get("expiry_date_distance")),
                self._f(quote.get("open_interest")), self._f(quote.get("recovery_price")),
                self._f(quote.get("price_recovery_ratio")), self._f(quote.get("pre_price")),
                self._f(quote.get("after_price")), quote.get("regime", ""),
                strategy_name, json.dumps(kwargs, ensure_ascii=False) if kwargs else ""
            ))
            self._safe_commit()
        except Exception as e:
            logger.warning(f"[DB] save_quote_snapshot 失败 {symbol}: {e}")

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

    # ========== 参数历史 ==========

    def get_latest_params(self, vt_symbol: str, class_name: str) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT params FROM param_history WHERE vt_symbol=? AND class_name=? ORDER BY version DESC LIMIT 1", (vt_symbol, class_name))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def get_param_version(self, vt_symbol: str, class_name: str, version: int) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT params FROM param_history WHERE vt_symbol=? AND class_name=? AND version=?", (vt_symbol, class_name, version))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def get_param_history(self, vt_symbol: str, class_name: str, limit: int = 20) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM param_history WHERE vt_symbol=? AND class_name=? ORDER BY version DESC LIMIT ?", (vt_symbol, class_name, limit))
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if isinstance(d.get("params"), str):
                try:
                    d["params"] = json.loads(d["params"])
                except Exception:
                    d["params"] = {}
            result.append(d)
        return result

    def _save_param_history(self, vt_symbol: str, class_name: str, params: dict, version: int):
        cursor = self.conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO param_history (vt_symbol, class_name, params, version, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (vt_symbol, class_name, json.dumps(params, ensure_ascii=False), version, now))
        self._safe_commit()

    # ========== 部署日志 & 事件 ==========

    def log_deploy(self, strategy_name: str, version: int, action: str,
                   operator: str, result: str, message: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO deploy_log (strategy_name, version, action, operator, result, message, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (strategy_name, version, action, operator, result, message, now))
        self._safe_commit()

    def log_event(self, timestamp: str, level: str, message: str, strategy_name: str = ""):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO events (timestamp, level, message, strategy_name)
            VALUES (?,?,?,?)
        """, (timestamp, level, message, strategy_name))
        self._safe_commit()

    # ========== Regime 查询 ==========

    def get_latest_regime(self, symbol: str, exchange: str = "SMART") -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT regime, prob_trend, prob_range, prob_volatile, confidence, features_json
            FROM regime_records WHERE symbol=? AND exchange=?
            ORDER BY rowid DESC LIMIT 1
        """, (symbol, exchange))
        row = cursor.fetchone()
        if not row:
            return None
        try:
            feats = json.loads(row[5]) if row[5] else {}
        except (json.JSONDecodeError, TypeError):
            feats = {}
        return {
            "regime": row[0], "prob_trend": row[1],
            "prob_range": row[2], "prob_volatile": row[3],
            "confidence": row[4], "features": feats,
        }

    def close(self):
        self.conn.close()

    # ========== 启动恢复 ==========

    def get_running_strategies_for_restart(self) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT r.strategy_name, r.run_id, r.class_name, r.vt_symbol,
                   r.market, r.params_json, sc.params as sc_params
            FROM strategy_runs r
            LEFT JOIN strategy_config sc ON sc.strategy_name = r.strategy_name
            WHERE r.status = 'RUNNING'
            ORDER BY r.started_at DESC
        """)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = []
        for row in rows:
            d = dict(zip(columns, row))
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
