"""
multi_period_db.py — 多周期K线数据库 v2.7.0
功能：SQLite存储/读取K线，管理ai_stock_pool选股池，自动兼容旧表结构
版本：v2.7.0
变更：2026-07-26 新增 get_pool / get_all_strategies 方法，修复RemoteController调用
"""

import sqlite3
import json
import pandas as pd
from datetime import datetime
import pytz

CHINA_TZ = pytz.timezone("Asia/Shanghai")


class MultiPeriodDB:
    def __init__(self, db_path: str = "data/apollo.db"):
        self.db_path = db_path
        self._ensure_tables()

    def _connect(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _ensure_tables(self):
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_stock_pool (
                    stock_code TEXT PRIMARY KEY,
                    market TEXT, score REAL, reason TEXT,
                    indicators TEXT, expires_at TEXT,
                    status TEXT DEFAULT 'selected',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for table in ["kline_1m","kline_5m","kline_15m","kline_60m","kline_1d"]:
                cur.execute(f"PRAGMA table_info({table})")
                cols = [r[1] for r in cur.fetchall()]
                if cols and "time" not in cols:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")
                    conn.commit()
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        symbol TEXT NOT NULL, time TIMESTAMP NOT NULL,
                        open REAL, high REAL, low REAL, close REAL,
                        volume INTEGER, turnover REAL,
                        PRIMARY KEY (symbol, time)
                    )
                """)
            conn.commit()

    def save_bars(self, symbol, period, df):
        if df.empty:
            return
        with self._connect() as conn:
            for _, row in df.iterrows():
                conn.execute(f"""
                    INSERT OR REPLACE INTO kline_{period}
                    (symbol,time,open,high,low,close,volume,turnover)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (symbol, int(pd.to_datetime(row["time_key"]).timestamp()),
                      float(row["open"]), float(row["high"]),
                      float(row["low"]), float(row["close"]),
                      float(row["volume"]), float(row.get("turnover",0))))
            conn.commit()

    def load_bars(self, symbol, period, limit=200):
        if not isinstance(symbol, str):
            return pd.DataFrame()
        try:
            with self._connect() as conn:
                df = pd.read_sql(
                    f"SELECT * FROM kline_{period} WHERE symbol=? ORDER BY time DESC LIMIT ?",
                    conn, params=(symbol, limit))
                if not df.empty:
                    df["time"] = pd.to_datetime(df["time"])
                    if df["time"].dt.tz is None:
                        df["time"] = df["time"].dt.tz_localize(CHINA_TZ)
                    df = df.iloc[::-1].reset_index(drop=True)
                return df
        except Exception as e:
            print(f"[DB ERROR] {symbol} {period}: {e}")
            return pd.DataFrame()

    def add_to_pool(self, records):
        with self._connect() as conn:
            for r in records:
                conn.execute("""
                    INSERT OR REPLACE INTO ai_stock_pool
                    (stock_code,market,score,reason,indicators,expires_at,status)
                    VALUES (?,?,?,?,?,?,?)
                """, (r["stock_code"], r.get("market","US"),
                      r.get("score",0), r.get("reason",""),
                      json.dumps(r.get("indicators",{})),
                      r.get("expires_at",""), r.get("status","selected")))
            conn.commit()
        print(f"[DB] ✅ 写入选股池: {len(records)} 条")

    # ---------- 新增方法：供 RemoteController 调用 ----------
    def get_pool(self, limit=20):
        """返回选股池记录列表（每条为字典）"""
        with self._connect() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM ai_stock_pool ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]

    def get_all_strategies(self):
        """占位方法：返回空列表（待策略引擎接入后实现）"""
        return []

    def get_active_strategies(self):
        """占位方法：返回空列表"""
        return []