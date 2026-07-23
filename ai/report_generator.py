"""
ai/report_generator.py - v2.6.0
日报/报告生成模块（线程安全版）
每次查询创建独立 SQLite 连接，避免跨线程错误
"""
import os
import datetime
import sqlite3
import pandas as pd


class ReportGenerator:
    def __init__(self, db_manager):
        if isinstance(db_manager, str):
            self.db_path = db_manager
        else:
            self.db_path = getattr(db_manager, "db_path", "data/database/trade.db")
        d = os.path.dirname(self.db_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def generate_daily(self) -> str:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "📊 <b>Apollo AI Trader 日报</b>",
            f"📅 {today}",
            "",
        ]

        try:
            conn = self._connect()
            try:
                df = pd.read_sql_query(
                    "SELECT code, name, score FROM stock_selections "
                    "WHERE date(timestamp)=date('now') ORDER BY score DESC LIMIT 10",
                    conn
                )
                if not df.empty:
                    lines.append("🏆 <b>今日AI选股 Top 10</b>")
                    for i, r in df.iterrows():
                        lines.append(f"  {i+1}. {r['code']} {r.get('name','')} | 评分 {float(r['score']):.1f}")
                else:
                    lines.append("🏆 今日选股：暂无数据")
            finally:
                conn.close()
        except Exception as e:
            lines.append(f"⚠️ 选股读取失败: {e}")

        lines.append("")

        try:
            conn = self._connect()
            try:
                df = pd.read_sql_query(
                    "SELECT text FROM events WHERE date(timestamp)=date('now') ORDER BY id DESC LIMIT 10",
                    conn
                )
                if not df.empty:
                    lines.append("📋 <b>今日事件</b>")
                    for _, r in df.iterrows():
                        lines.append(f"  • {r['text']}")
                else:
                    lines.append("📋 今日事件：无")
            finally:
                conn.close()
        except Exception as e:
            lines.append(f"⚠️ 事件读取失败: {e}")

        lines += ["", f"🕐 生成时间: {now}", "━━━━━━━━━━━━━━━━",
                  "⚠️ 本系统仅供模拟测试，不构成投资建议"]
        return "\n".join(lines)

    def generate_weekly(self) -> str:
        return self.generate_daily()

    def generate_trade_summary(self, symbol: str = "") -> str:
        return f"📈 {symbol} 交易摘要：功能开发中"
