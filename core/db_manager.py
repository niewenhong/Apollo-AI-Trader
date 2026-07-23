"""
core/db_manager.py - Apollo Trader v2.6.0
自定义数据库管理：AI选股/诊股/参数/审核/回测/执行池
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict


class CustomDBManager:
    """管理 vnpy 标准表之外的自定义数据表"""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = Path.home() / ".vntrader" / "database.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._init_tables()

    @property
    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _init_tables(self):
        c = self.conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS ai_stock_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                score REAL NOT NULL,
                reason TEXT DEFAULT '',
                indicators_json TEXT DEFAULT '{}',
                selected_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                status TEXT DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_pool_score ON ai_stock_pool(score DESC);

            CREATE TABLE IF NOT EXISTS stock_diagnosis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                diagnosis_json TEXT NOT NULL,
                summary TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS param_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                strategy_class TEXT NOT NULL,
                params_json TEXT NOT NULL,
                source TEXT DEFAULT 'ai',
                confidence REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now')),
                applied INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS param_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                strategy_class TEXT NOT NULL,
                params_json TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS review_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                strategy_class TEXT,
                decision TEXT NOT NULL,
                reason TEXT DEFAULT '',
                metrics_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT NOT NULL,
                strategy_class TEXT NOT NULL,
                params_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                validated INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS execution_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vt_symbol TEXT UNIQUE NOT NULL,
                market TEXT NOT NULL,
                strategy_class TEXT NOT NULL,
                params_json TEXT DEFAULT '{}',
                added_at TEXT DEFAULT (datetime('now')),
                last_review_at TEXT,
                status TEXT DEFAULT 'active'
            );
        """)
        self.conn.commit()

    # ── AI选股池 ──
    def save_stock_pool(self, pool):
        c = self.conn.cursor()
        count = 0
        for item in pool:
            try:
                c.execute(
                    "INSERT OR REPLACE INTO ai_stock_pool "
                    "(vt_symbol,market,score,reason,indicators_json,expires_at,status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (item["vt_symbol"], item.get("market",""),
                     float(item.get("score",0)), item.get("reason",""),
                     json.dumps(item.get("indicators",{})),
                     item.get("expires_at"), item.get("status","selected")))
                count += 1
            except Exception as e:
                print(f"[DB] save_stock_pool error: {e}")
        self.conn.commit()
        return count

    def get_top_pool(self, n=25, market=None):
        c = self.conn.cursor()
        if market:
            rows = c.execute(
                "SELECT * FROM ai_stock_pool WHERE market=? ORDER BY score DESC LIMIT ?",
                (market, n)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM ai_stock_pool ORDER BY score DESC LIMIT ?",
                (n,)).fetchall()
        return [dict(r) for r in rows]

    # ── 诊股 ──
    def save_diagnosis(self, vt_symbol, diagnosis, summary=""):
        self.conn.execute(
            "INSERT INTO stock_diagnosis (vt_symbol,diagnosis_json,summary) VALUES (?,?,?)",
            (vt_symbol, json.dumps(diagnosis, ensure_ascii=False), summary))
        self.conn.commit()

    def get_latest_diagnosis(self, vt_symbol):
        row = self.conn.execute(
            "SELECT * FROM stock_diagnosis WHERE vt_symbol=? ORDER BY created_at DESC LIMIT 1",
            (vt_symbol,)).fetchone()
        return dict(row) if row else None

    # ── 参数建议 ──
    def save_param_suggestion(self, vt_symbol, strategy_class, params, source="ai", conf=0.0):
        self.conn.execute(
            "INSERT INTO param_suggestions (vt_symbol,strategy_class,params_json,source,confidence) VALUES (?,?,?,?,?)",
            (vt_symbol, strategy_class, json.dumps(params), source, conf))
        self.conn.commit()

    def get_latest_params(self, vt_symbol, strategy_class):
        row = self.conn.execute(
            "SELECT params_json FROM param_suggestions WHERE vt_symbol=? AND strategy_class=? ORDER BY created_at DESC LIMIT 1",
            (vt_symbol, strategy_class)).fetchone()
        return json.loads(row[0]) if row else None

    def save_param_history(self, vt_symbol, strategy_class, params, source="manual"):
        self.conn.execute(
            "INSERT INTO param_history (vt_symbol,strategy_class,params_json,source) VALUES (?,?,?,?)",
            (vt_symbol, strategy_class, json.dumps(params), source))
        self.conn.commit()

    # ── 审核决策 ──
    def save_review(self, vt_symbol, decision, strategy_class="", reason="", metrics=None):
        self.conn.execute(
            "INSERT INTO review_decisions (vt_symbol,strategy_class,decision,reason,metrics_json) VALUES (?,?,?,?,?)",
            (vt_symbol, strategy_class, decision, reason, json.dumps(metrics or {})))
        self.conn.commit()

    # ── 回测结果 ──
    def save_backtest(self, vt_symbol, strategy_class, params, metrics, validated=False):
        self.conn.execute(
            "INSERT INTO backtest_results (vt_symbol,strategy_class,params_json,metrics_json,validated) VALUES (?,?,?,?,?)",
            (vt_symbol, strategy_class, json.dumps(params), json.dumps(metrics), int(validated)))
        self.conn.commit()

    def get_best_params(self, vt_symbol, strategy_class):
        row = self.conn.execute(
            "SELECT params_json FROM backtest_results WHERE vt_symbol=? AND strategy_class=? ORDER BY json_extract(metrics_json,'$.sharpe_ratio') DESC LIMIT 1",
            (vt_symbol, strategy_class)).fetchone()
        return json.loads(row[0]) if row else None

    # ── 执行池 ──
    def add_to_pool(self, vt_symbol, market, strategy_class, params=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO execution_pool (vt_symbol,market,strategy_class,params_json) VALUES (?,?,?,?)",
            (vt_symbol, market, strategy_class, json.dumps(params or {})))
        self.conn.commit()

    def get_pool(self, market=None):
        c = self.conn.cursor()
        if market:
            rows = c.execute(
                "SELECT * FROM execution_pool WHERE market=? AND status='active'",
                (market,)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM execution_pool WHERE status='active'").fetchall()
        return [dict(r) for r in rows]

    def remove_from_pool(self, vt_symbol):
        self.conn.execute(
            "UPDATE execution_pool SET status='removed' WHERE vt_symbol=?",
            (vt_symbol,))
        self.conn.commit()