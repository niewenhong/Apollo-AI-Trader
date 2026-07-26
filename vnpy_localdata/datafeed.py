"""
vnpy_localdata/datafeed.py - 本地数据服务适配器（修正版 v2）
直接从本地 SQLite 数据库查询 K 线数据
表名格式：bar_{interval_value}，例如 bar_1m, bar_5m, bar_15m, bar_60m, bar_day, bar_week
"""
import logging
import sqlite3
import os
from datetime import datetime
from typing import Optional, List, Callable

from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.object import BarData, TickData, HistoryRequest
from vnpy.trader.constant import Exchange, Interval

logger = logging.getLogger("LocalDatafeed")

DB_PATH = os.environ.get("APOLLO_DATA_DB", "data/apollo.db")


class LocalDatafeed(BaseDatafeed):
    """本地数据服务"""

    def __init__(self):
        super().__init__()
        self.initialized = False
        self.conn = None

    def init(self, output: Callable = print) -> bool:
        try:
            if not os.path.exists(DB_PATH):
                output(f"⚠️ 本地数据库不存在: {DB_PATH}")
                self._ensure_schema()
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.initialized = True
            output(f"✅ 本地数据服务初始化成功: {DB_PATH}")
            return True
        except Exception as e:
            output(f"❌ 本地数据服务初始化失败: {e}")
            return False

    def query_bar_history(self, req: HistoryRequest,
                          output: Callable = print) -> Optional[List[BarData]]:
        if not self.initialized:
            if not self.init(output):
                return None

        symbol = req.symbol
        exchange = req.exchange
        interval = req.interval
        start = req.start
        end = req.end

        vt_symbol = f"{symbol}.{exchange.value}"

        # 使用 interval.value 作为表后缀，例如 "1m", "5m", "15m", "60m", "day", "week"
        table_name = f"bar_{interval.value}"

        try:
            cursor = self.conn.cursor()
            cursor.execute(f"""
                SELECT datetime, open, high, low, close, volume, turnover
                FROM {table_name}
                WHERE vt_symbol = ?
                  AND datetime >= ?
                  AND datetime <= ?
                ORDER BY datetime ASC
            """, (vt_symbol, start, end))

            rows = cursor.fetchall()

            if not rows:
                output(f"⚠️ 本地无数据: {vt_symbol} [{interval.value}]")
                return []

            bars = []
            for row in rows:
                dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
                bar = BarData(
                    symbol=symbol,
                    exchange=exchange,
                    datetime=dt,
                    interval=interval,
                    open_price=row["open"],
                    high_price=row["high"],
                    low_price=row["low"],
                    close_price=row["close"],
                    volume=row["volume"],
                    turnover=row["turnover"],
                    gateway_name="LOCAL",
                )
                bars.append(bar)

            output(f"✅ 从本地数据库读取 {len(bars)} 根 K 线: {vt_symbol} [{interval.value}]")
            return bars

        except Exception as e:
            output(f"❌ 查询本地数据库失败: {e}")
            return None

    def query_tick_history(self, req: HistoryRequest,
                           output: Callable = print) -> Optional[List[TickData]]:
        output("ℹ️ 本地数据服务暂不支持 Tick 查询")
        return []

    def _ensure_schema(self):
        """创建默认的表结构（如果不存在）"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # 预创建常见周期的表
        for suffix in ["1m", "5m", "15m", "30m", "60m", "day", "week"]:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS bar_{suffix} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vt_symbol TEXT NOT NULL,
                    datetime TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, turnover REAL,
                    UNIQUE(vt_symbol, datetime)
                )
            """)
            cursor.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_bar_{suffix}_vt_datetime
                ON bar_{suffix}(vt_symbol, datetime)
            """)
        conn.commit()
        conn.close()