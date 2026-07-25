# utils/database.py
import sqlite3
import os
from datetime import datetime

class Database:
    def __init__(self, db_path='apollo_trader.db'):
        self.db_path = os.path.join(os.path.dirname(__file__), '..', db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._create_table()

    def _create_table(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                message TEXT,
                strategy TEXT
            )
        ''')
        self.conn.commit()

    def log_event(self, timestamp, level, msg, strategy_name=""):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO events (timestamp, level, message, strategy) VALUES (?, ?, ?, ?)",
            (timestamp, level, msg, strategy_name)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()