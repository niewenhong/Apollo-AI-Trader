"""
core/db_manager.py - Apollo Trader v2.7.0
数据库管理器（完整版，含选股池和AI参数方法）
"""
import sqlite3
import json
import time
import os
import threading
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("DBManager")


class CustomDBManager:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'apollo.db')
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                strategy_name TEXT PRIMARY KEY,
                class_name TEXT NOT NULL,
                vt_symbol TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'US',
                params TEXT NOT NULL DEFAULT '{}',
                current_version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT (julianday('now')),
                updated_at REAL NOT NULL DEFAULT (julianday('now')),
                source TEXT NOT NULL DEFAULT 'manual',
                modified_by TEXT NOT NULL DEFAULT 'system',
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS param_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                class_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                params TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (julianday('now')),
                created_by TEXT NOT NULL DEFAULT 'system',
                UNIQUE(vt_symbol, class_name, version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deploy_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                action TEXT NOT NULL,
                operator TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT (julianday('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                strategy_name TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_stock_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                market TEXT NOT NULL DEFAULT 'US',
                score REAL DEFAULT 0.0,
                reason TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT (julianday('now')),
                batch_id TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_params_cache (
                vt_symbol TEXT NOT NULL,
                class_name TEXT NOT NULL,
                params TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (julianday('now')),
                PRIMARY KEY (vt_symbol, class_name)
            )
        """)
        conn.commit()

    # ── 策略 CRUD ──
    def save_strategy(self, strategy_name: str, class_name: str, vt_symbol: str, market: str,
                      params: dict, source: str = "manual", modifier: str = "system") -> Tuple[int, int]:
        conn = self._get_conn()
        now = time.time()
        params_json = json.dumps(params, ensure_ascii=False)
        
        existing = conn.execute(
            "SELECT current_version FROM strategies WHERE strategy_name = ?",
            (strategy_name,)
        ).fetchone()
        
        if existing:
            new_version = existing[0] + 1
            conn.execute("""
                UPDATE strategies SET 
                    class_name = ?, vt_symbol = ?, market = ?, params = ?,
                    current_version = ?, updated_at = ?, source = ?, modified_by = ?
                WHERE strategy_name = ?
            """, (class_name, vt_symbol, market, params_json, new_version, now, source, modifier, strategy_name))
            is_new = False
        else:
            new_version = 1
            conn.execute("""
                INSERT INTO strategies 
                    (strategy_name, class_name, vt_symbol, market, params, current_version,
                     created_at, updated_at, source, modified_by, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (strategy_name, class_name, vt_symbol, market, params_json, new_version,
                  now, now, source, modifier))
            is_new = True
        
        conn.execute("""
            INSERT OR REPLACE INTO param_versions 
                (vt_symbol, class_name, version, params, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (vt_symbol, class_name, new_version, params_json, now, modifier))
        
        conn.commit()
        return (int(is_new), new_version)

    def get_strategy(self, strategy_name: str) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT * FROM strategies WHERE strategy_name = ?", (strategy_name,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d.get("params", "{}"))
        return d

    def get_all_strategies(self, enabled_only: bool = False) -> List[dict]:
        if enabled_only:
            rows = self._get_conn().execute(
                "SELECT * FROM strategies WHERE enabled = 1 ORDER BY strategy_name"
            ).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM strategies ORDER BY strategy_name"
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["params"] = json.loads(d.get("params", "{}"))
            result.append(d)
        return result

    def disable_strategy(self, strategy_name: str):
        self._get_conn().execute(
            "UPDATE strategies SET enabled = 0, updated_at = ? WHERE strategy_name = ?",
            (time.time(), strategy_name)
        )
        self._get_conn().commit()

    def mark_deployed(self, strategy_name: str, version: int, operator: str = "system"):
        self._get_conn().execute(
            "UPDATE strategies SET current_version = ?, updated_at = ? WHERE strategy_name = ?",
            (version, time.time(), strategy_name)
        )
        self._get_conn().commit()

    def detect_changed_strategies(self, deployed_timestamps: Dict[str, float]) -> List[dict]:
        if not deployed_timestamps:
            return []
        names = list(deployed_timestamps.keys())
        placeholders = ','.join(['?'] * len(names))
        rows = self._get_conn().execute(
            f"SELECT * FROM strategies WHERE strategy_name IN ({placeholders}) AND updated_at > ?",
            (*names, min(deployed_timestamps.values()))
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["params"] = json.loads(d.get("params", "{}"))
            if d["enabled"] == 0:
                d["_change_type"] = "disabled"
            else:
                d["_change_type"] = "updated"
            result.append(d)
        return result

    def log_deploy(self, strategy_name: str, version: int, action: str, operator: str,
                   status: str, message: str = ""):
        self._get_conn().execute(
            "INSERT INTO deploy_log (strategy_name, version, action, operator, status, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (strategy_name, version, action, operator, status, message)
        )
        self._get_conn().commit()

    def log_event(self, timestamp: str, level: str, message: str, strategy_name: str = ""):
        self._get_conn().execute(
            "INSERT INTO events (timestamp, level, message, strategy_name) VALUES (?, ?, ?, ?)",
            (timestamp, level, message, strategy_name)
        )
        self._get_conn().commit()

    # ── 参数版本 ──
    def get_param_version(self, vt_symbol: str, class_name: str, version: int) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT * FROM param_versions WHERE vt_symbol = ? AND class_name = ? AND version = ?",
            (vt_symbol, class_name, version)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["params"])

    def get_param_history(self, vt_symbol: str, class_name: str, limit: int = 20) -> List[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM param_versions WHERE vt_symbol = ? AND class_name = ? "
            "ORDER BY version DESC LIMIT ?",
            (vt_symbol, class_name, limit)
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["params"] = json.loads(d["params"])
            result.append(d)
        return result

    def get_active_strategies(self) -> List[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM strategies WHERE enabled = 1 ORDER BY strategy_name"
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["params"] = json.loads(d.get("params", "{}"))
            result.append(d)
        return result

    # ── 选股池 ──
    def add_to_pool(self, stocks, *args, batch_id=None):
        """
        将选股结果写入 ai_stock_pool
        支持两种输入格式：
        - 字符串列表: ["00700", "AAPL", ...]
        - 字典列表: [{"stock_code":"00700", "stock_name":"腾讯", ...}, ...]
        """
        conn = self._get_conn()
        now = time.time()
        if batch_id is None:
            batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        records = []
        for item in stocks:
            if isinstance(item, str):
                # 纯字符串：视为股票代码
                record = {
                    "stock_code": item,
                    "stock_name": "",
                    "market": "US",  # 默认市场，可由调用方自行调整
                    "score": 0.0,
                    "reason": ""
                }
            else:
                record = item
            records.append(record)
        
        for s in records:
            conn.execute("""
                INSERT INTO ai_stock_pool 
                    (stock_code, stock_name, market, score, reason, created_at, batch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                s.get("stock_code", ""),
                s.get("stock_name", ""),
                s.get("market", "US"),
                s.get("score", 0.0),
                s.get("reason", ""),
                now,
                batch_id
            ))
        conn.commit()
        logger.info(f"[DB] 选股池已写入 {len(records)} 条记录，批次: {batch_id}")

    def get_pool(self, market: str = None, limit: int = 50) -> List[dict]:
        conn = self._get_conn()
        if market:
            rows = conn.execute(
                "SELECT * FROM ai_stock_pool WHERE market = ? ORDER BY created_at DESC LIMIT ?",
                (market, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ai_stock_pool ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_pool(self, batch_id: str = None):
        conn = self._get_conn()
        if batch_id:
            conn.execute("DELETE FROM ai_stock_pool WHERE batch_id = ?", (batch_id,))
        else:
            conn.execute("DELETE FROM ai_stock_pool")
        conn.commit()

    # ── AI参数缓存 ──
    def save_latest_params(self, vt_symbol: str, class_name: str, params: dict):
        conn = self._get_conn()
        params_json = json.dumps(params, ensure_ascii=False)
        conn.execute("""
            INSERT OR REPLACE INTO ai_params_cache (vt_symbol, class_name, params, created_at)
            VALUES (?, ?, ?, ?)
        """, (vt_symbol, class_name, params_json, time.time()))
        conn.commit()

    def get_latest_params(self, vt_symbol: str, class_name: str) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT * FROM ai_params_cache WHERE vt_symbol = ? AND class_name = ?",
            (vt_symbol, class_name)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["params"])