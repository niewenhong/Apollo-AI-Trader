"""
core/db_manager.py - v2.6.0
数据库管理器：SQLite存储选股结果、事件日志等
"""
import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class CustomDBManager:
    """自定义数据库管理器"""

    def __init__(self, db_path: str = "data/database/trade.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self._ensure_directory()
        self._connect()
        self.init_db()

    def _ensure_directory(self):
        """确保数据库文件所在的目录存在"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"创建数据库目录: {db_dir}")

    def _connect(self):
        """建立数据库连接"""
        try:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            logger.info(f"数据库连接成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            raise

    def init_db(self):
        """初始化数据库表结构"""
        if not self.conn:
            return
        cursor = self.conn.cursor()

        # 选股结果表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT DEFAULT '',
                score REAL DEFAULT 0,
                timestamp TEXT NOT NULL
            )
        """)

        # 事件日志表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                timestamp TEXT
            )
        """)

        # 策略运行记录表（可选）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT,
                status TEXT,
                started_at TEXT,
                stopped_at TEXT
            )
        """)

        self.conn.commit()
        logger.info("数据库表结构初始化完成")

    def save_stock_selection(self, record: dict):
        """
        保存选股结果
        record: {"code": str, "name": str, "score": float, "timestamp": str}
        """
        if not self.conn:
            logger.warning("数据库未连接，无法保存选股结果")
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO stock_selections (code, name, score, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (record["code"], record["name"], record["score"], record["timestamp"])
            )
            self.conn.commit()
            logger.info(f"选股记录已保存: {record['code']} 评分 {record['score']}")
        except Exception as e:
            logger.error(f"保存选股记录失败: {e}")

    def log_event(self, event_text: str):
        """记录事件日志"""
        if not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO events (text, timestamp) VALUES (?, ?)",
                (event_text, datetime.now().isoformat())
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"记录事件失败: {e}")

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            try:
                self.conn.close()
                logger.info("数据库连接已关闭")
            except Exception as e:
                logger.error(f"关闭数据库连接失败: {e}")
            finally:
                self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()