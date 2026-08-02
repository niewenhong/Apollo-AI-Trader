"""
core/db_manager.py - Apollo Trader v3.1.6 全功能数据库管理器（修复事务问题）
"""
import sqlite3
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger("DBManager")


class DBManager:
    """数据库管理器 - 支持 strategy_config / tick_data / quote_snapshot / ai_stock_pool / stock_diagnosis / deploy_log / events"""

    def __init__(self, db_path: str = "data/history.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 启用自动事务模式（Python sqlite3 默认 autocommit=False，需显式 BEGIN/COMMIT）
        self.conn.isolation_level = None  # 使用自动提交模式，避免事务混乱
        self._create_tables()
        self._migrate_strategy_config()

    def _ensure_transaction(self):
        """确保有活跃事务，如果没有则开启一个"""
        try:
            # 尝试执行一条无害语句，如果不在事务中会抛出异常
            self.conn.execute("SELECT 1")
        except sqlite3.OperationalError:
            # 无活跃事务，重新连接或开启新事务
            self.conn.execute("BEGIN")

    def _safe_commit(self):
        """安全提交，避免 'no transaction is active' 错误"""
        try:
            self.conn.commit()
        except sqlite3.OperationalError as e:
            if "no transaction" in str(e):
                # 无事务可提交，忽略
                pass
            else:
                raise

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
                symbol TEXT NOT NULL, market TEXT DEFAULT 'US',
                score REAL DEFAULT 0, reason TEXT DEFAULT '',
                source TEXT DEFAULT 'selector', created_at TEXT DEFAULT ''
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
        self._safe_commit()
        logger.info(f"[DB INIT] ✅ 数据库表创建/验证完成 ({self.db_path})")

    def _migrate_strategy_config(self):
        """自动补列"""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(strategy_config)")
        cols = {row[1] for row in cursor.fetchall()}
        additions = {
            'status': "ALTER TABLE strategy_config ADD COLUMN status TEXT DEFAULT 'PENDING'",
            'status_msg': "ALTER TABLE strategy_config ADD COLUMN status_msg TEXT DEFAULT ''",
            'enabled': "ALTER TABLE strategy_config ADD COLUMN enabled INTEGER DEFAULT 1",
            'active': "ALTER TABLE strategy_config ADD COLUMN active INTEGER DEFAULT 1",
            'current_version': "ALTER TABLE strategy_config ADD COLUMN current_version INTEGER DEFAULT 1",
            'version': "ALTER TABLE strategy_config ADD COLUMN version INTEGER DEFAULT 1",
        }
        for col, ddl in additions.items():
            if col not in cols:
                try:
                    cursor.execute(ddl)
                    self._safe_commit()
                    logger.info(f"[DB] 迁移: strategy_config 添加 {col} 列")
                except Exception:
                    pass

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

    # ========== 选股池 & 诊断 ==========

    def add_to_pool(self, symbol: str, score: float, reason: str = "",
                    market: str = "US", source: str = "selector") -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO ai_stock_pool
                (symbol, market, score, reason, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (symbol, market, score, reason, source,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self._safe_commit()
            return True
        except Exception as e:
            logger.error(f"[DB] add_to_pool 失败 {symbol}: {e}")
            return False

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
                VALUES (?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?)
            """, (
                symbol,
                quote.get("underlying", ""),
                now,
                trigger_type,
                self._f(quote.get("last_price")),
                self._f(quote.get("open_price")),
                self._f(quote.get("high_price")),
                self._f(quote.get("low_price")),
                self._f(quote.get("prev_close")),
                self._f(quote.get("volume")),
                self._f(quote.get("turnover")),
                self._f(quote.get("implied_volatility")),
                self._f(quote.get("delta")),
                self._f(quote.get("gamma")),
                self._f(quote.get("vega")),
                self._f(quote.get("theta")),
                self._f(quote.get("rho")),
                self._f(quote.get("premium")),
                self._f(quote.get("strike_price")),
                self._f(quote.get("expiry_date_distance")),
                self._f(quote.get("open_interest")),
                self._f(quote.get("recovery_price")),
                self._f(quote.get("price_recovery_ratio")),
                self._f(quote.get("pre_price")),
                self._f(quote.get("after_price")),
                quote.get("regime", ""),
                strategy_name,
                json.dumps(kwargs, ensure_ascii=False) if kwargs else ""
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
        cursor.execute("""
            SELECT params FROM param_history
            WHERE vt_symbol=? AND class_name=?
            ORDER BY version DESC LIMIT 1
        """, (vt_symbol, class_name))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def get_param_version(self, vt_symbol: str, class_name: str, version: int) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT params FROM param_history
            WHERE vt_symbol=? AND class_name=? AND version=?
        """, (vt_symbol, class_name, version))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def get_param_history(self, vt_symbol: str, class_name: str, limit: int = 20) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM param_history
            WHERE vt_symbol=? AND class_name=?
            ORDER BY version DESC LIMIT ?
        """, (vt_symbol, class_name, limit))
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

    def close(self):
        self.conn.close()