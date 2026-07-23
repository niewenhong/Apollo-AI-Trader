"""
core/db_manager.py - Apollo-AI-Trader v2.6.0
自定义数据库管理：AI选股结果、诊股、参数建议/历史、审核决策、回测结果
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any


class CustomDBManager:
    """管理自定义数据表（非vnpy标准表），所有表统一存放在 .vntrader/apollo.db"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            try:
                from vnpy.trader.setting import SETTINGS
                p = SETTINGS.get("database.database", "database.db")
                self.db_path = Path.home() / ".vntrader" / Path(p).parent / "apollo.db"
            except ImportError:
                self.db_path = Path.home() / ".vntrader" / "apollo.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._init_tables()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 建表 ──────────────────────────────────────────
    def _init_tables(self):
        c = self.conn.cursor()
        # AI选股池
        c.execute("""CREATE TABLE IF NOT EXISTS ai_stock_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, market TEXT NOT NULL,
            score REAL NOT NULL, reason TEXT, indicators_json TEXT,
            selected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP, status TEXT DEFAULT 'pending',
            UNIQUE(vt_symbol, selected_at))""")
        # 诊股结果
        c.execute("""CREATE TABLE IF NOT EXISTS stock_diagnosis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, diagnosis_json TEXT NOT NULL,
            summary TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # 参数建议
        c.execute("""CREATE TABLE IF NOT EXISTS param_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, strategy_class TEXT NOT NULL,
            params_json TEXT NOT NULL, source TEXT DEFAULT 'ai',
            confidence REAL DEFAULT 0.0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            applied BOOLEAN DEFAULT 0)""")
        # 参数历史
        c.execute("""CREATE TABLE IF NOT EXISTS param_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, strategy_class TEXT NOT NULL,
            params_json TEXT NOT NULL, source TEXT DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # 审核决策
        c.execute("""CREATE TABLE IF NOT EXISTS review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, decision TEXT NOT NULL,
            reason TEXT, metrics_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # 回测结果
        c.execute("""CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, strategy_class TEXT NOT NULL,
            params_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
            validated BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        # 执行池
        c.execute("""CREATE TABLE IF NOT EXISTS execution_pool (
            vt_symbol TEXT PRIMARY KEY, market TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            capital_allocated REAL DEFAULT 0, status TEXT DEFAULT 'active')""")
        # 配额追踪
        c.execute("""CREATE TABLE IF NOT EXISTS quota_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vt_symbol TEXT NOT NULL, request_type TEXT NOT NULL,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            details TEXT)""")
        self.conn.commit()

    # ── AI选股 ────────────────────────────────────────
    def save_stock_pool(self, pool: List[Dict]) -> int:
        c = self.conn.cursor(); count = 0
        for item in pool:
            try:
                c.execute("""INSERT OR REPLACE INTO ai_stock_pool
                    (vt_symbol,market,score,reason,indicators_json,expires_at,status)
                    VALUES (?,?,?,?,?,?,?)""",
                    (item["vt_symbol"], item.get("market",""), item.get("score",0.0),
                     item.get("reason",""), json.dumps(item.get("indicators",{})),
                     item.get("expires_at"), item.get("status","pending")))
                count += 1
            except Exception as e:
                print(f"[DB] save_stock_pool error: {e}")
        self.conn.commit(); return count

    def get_active_pool(self) -> List[Dict]:
        now = datetime.now().isoformat()
        rows = self.conn.execute(
            "SELECT * FROM ai_stock_pool WHERE (expires_at IS NULL OR expires_at>?) ORDER BY score DESC",
            (now,)).fetchall()
        return [dict(r) for r in rows]

    # ── 诊股 ──────────────────────────────────────────
    def save_diagnosis(self, vt_symbol: str, diagnosis: dict, summary: str = ""):
        self.conn.execute(
            "INSERT INTO stock_diagnosis (vt_symbol,diagnosis_json,summary) VALUES (?,?,?)",
            (vt_symbol, json.dumps(diagnosis, ensure_ascii=False), summary))
        self.conn.commit()

    def get_latest_diagnosis(self, vt_symbol: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM stock_diagnosis WHERE vt_symbol=? ORDER BY created_at DESC LIMIT 1",
            (vt_symbol,)).fetchone()
        return dict(row) if row else None

    # ── 参数 ──────────────────────────────────────────
    def save_param_suggestion(self, vt_symbol, strategy_class, params, source="ai", conf=0.0):
        self.conn.execute(
            "INSERT INTO param_suggestions (vt_symbol,strategy_class,params_json,source,confidence) VALUES (?,?,?,?,?)",
            (vt_symbol, strategy_class, json.dumps(params), source, conf))
        self.conn.commit()

    def get_latest_params(self, vt_symbol, strategy_class) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT params_json FROM param_suggestions WHERE vt_symbol=? AND strategy_class=? ORDER BY created_at DESC LIMIT 1",
            (vt_symbol, strategy_class)).fetchone()
        return json.loads(row[0]) if row else None

    def save_param_history(self, vt_symbol, strategy_class, params, source="manual"):
        self.conn.execute(
            "INSERT INTO param_history (vt_symbol,strategy_class,params_json,source) VALUES (?,?,?,?)",
            (vt_symbol, strategy_class, json.dumps(params), source))
        self.conn.commit()

    def get_best_params(self, vt_symbol, strategy_class) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT params_json,metrics_json FROM backtest_results
               WHERE vt_symbol=? AND strategy_class=?
               ORDER BY json_extract(metrics_json,'$.sharpe_ratio') DESC LIMIT 1""",
            (vt_symbol, strategy_class)).fetchone()
        return json.loads(row[0]) if row else None

    # ── 审核 ──────────────────────────────────────────
    def save_review_decision(self, vt_symbol, decision, reason="", metrics=None):
        self.conn.execute(
            "INSERT INTO review_decisions (vt_symbol,decision,reason,metrics_json) VALUES (?,?,?,?)",
            (vt_symbol, decision, reason, json.dumps(metrics or {})))
        self.conn.commit()

    # ── 回测 ──────────────────────────────────────────
    def save_backtest_result(self, vt_symbol, strategy_class, params, metrics, validated=False):
        self.conn.execute(
            "INSERT INTO backtest_results (vt_symbol,strategy_class,params_json,metrics_json,validated) VALUES (?,?,?,?,?)",
            (vt_symbol, strategy_class, json.dumps(params), json.dumps(metrics), validated))
        self.conn.commit()

    # ── 执行池 ────────────────────────────────────────
    def add_to_pool(self, vt_symbol, market, capital=0):
        self.conn.execute(
            "INSERT OR REPLACE INTO execution_pool (vt_symbol,market,capital_allocated) VALUES (?,?,?)",
            (vt_symbol, market, capital))
        self.conn.commit()

    def get_execution_pool(self) -> List[Dict]:
        rows = self.conn.execute("SELECT * FROM execution_pool WHERE status='active'").fetchall()
        return [dict(r) for r in rows]

    # ── 配额 ──────────────────────────────────────────
    def log_quota(self, vt_symbol, req_type, details=""):
        self.conn.execute(
            "INSERT INTO quota_usage (vt_symbol,request_type,details) VALUES (?,?,?)",
            (vt_symbol, req_type, details))
        self.conn.commit()

    def get_quota_count(self, days=30) -> int:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM quota_usage WHERE used_at>?", (since,)).fetchone()
        return row[0] if row else 0
