"""
multi_period_db.py — 多周期数据库 v2.7.0
- 按周期分表: kline_1m / kline_5m / kline_15m / kline_60m / kline_1d
- 原生OHLCV原值存储，零损耗
"""

import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

PERIOD_TABLES = {
    "1m": "kline_1m", "5m": "kline_5m", "15m": "kline_15m",
    "60m": "kline_60m", "1d": "kline_1d",
}


class MultiPeriodDB:
    def __init__(self, db_path="data/history.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        for table in PERIOD_TABLES.values():
            self.cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    symbol TEXT, datetime TEXT,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, turnover REAL,
                    PRIMARY KEY (symbol, datetime))""")
        self.conn.commit()
        logger.info(f"✅ 多周期表已就绪: {list(PERIOD_TABLES.values())}")

    def save_bar(self, bar):
        """保存单根BAR（vn.py BarData对象）"""
        table = self._period_to_table(bar.interval, bar.window)
        if not table:
            return
        self.cursor.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?)",
            (bar.symbol,
             bar.datetime.strftime("%Y-%m-%d %H:%M:%S") if hasattr(bar.datetime, 'strftime') else str(bar.datetime),
             bar.open_price, bar.high_price, bar.low_price, bar.close_price,
             bar.volume, getattr(bar, 'turnover', 0) or 0))
        self.conn.commit()

    def save_bars(self, symbol, period, data_list):
        """批量保存（来自富途历史接口的数据）"""
        table = PERIOD_TABLES.get(period)
        if not table:
            return
        for d in data_list:
            dt = d.get("time_key") or d.get("datetime") or ""
            self.cursor.execute(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?)",
                (symbol, dt,
                 float(d.get("open", 0)), float(d.get("high", 0)),
                 float(d.get("low", 0)), float(d.get("close", 0)),
                 float(d.get("volume", 0)), float(d.get("turnover", 0))))
        self.conn.commit()

    def load_bars(self, symbol, period, start=None, end=None, limit=None):
        """加载历史BAR"""
        table = PERIOD_TABLES.get(period)
        if not table:
            return []
        sql = f"SELECT * FROM {table} WHERE symbol=?"
        params = [symbol]
        if start:
            sql += " AND datetime>=?"; params.append(start)
        if end:
            sql += " AND datetime<=?"; params.append(end)
        sql += " ORDER BY datetime"
        if limit:
            sql += f" LIMIT {int(limit)}"
        self.cursor.execute(sql, params)
        return self.cursor.fetchall()

    def find_gaps(self, symbol, period, start, end):
        """检测数据缺口"""
        bars = self.load_bars(symbol, period, start, end)
        if len(bars) < 2:
            return [(start, end)]
        gaps = []
        expected_min = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}.get(period, 1)
        for i in range(1, len(bars)):
            try:
                prev = datetime.strptime(bars[i-1][1], "%Y-%m-%d %H:%M:%S")
                curr = datetime.strptime(bars[i][1], "%Y-%m-%d %H:%M:%S")
            except:
                continue
            diff = (curr - prev).total_seconds() / 60
            if diff > expected_min * 1.5:
                gaps.append((bars[i-1][1], bars[i][1]))
        return gaps

    def _period_to_table(self, interval, window):
        if interval == "MINUTE":
            key = f"{window}m"
        elif interval == "HOUR":
            key = f"{window}h"
        else:
            key = "1d"
        return PERIOD_TABLES.get(key)
