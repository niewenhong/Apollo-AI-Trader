"""
ai/report_generator.py - v2.6.0
日报/报告生成模块（线程安全版）
"""
import os
import datetime
import sqlite3
import pandas as pd


class ReportGenerator:
    """
    报告生成器 - 每次查询创建独立 SQLite 连接
    彻底避免 'SQLite objects created in a thread can only be used in that same thread' 错误
    """

    def __init__(self, db_manager):
        """
        db_manager: CustomDBManager 实例
        从中获取 db_path，每次查询自己开新连接
        """
        # 兼容传入 CustomDBManager 或直接传入路径字符串
        if isinstance(db_manager, str):
            self.db_path = db_manager
        else:
            self.db_path = getattr(db_manager, 'db_path', 'data/database/trade.db')

        # 确保数据库目录存在
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def _connect(self):
        """每次创建新连接（线程安全）"""
        return sqlite3.connect(self.db_path)

    def generate_daily(self) -> str:
        """生成今日交易日报"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = []
        lines.append("📊 <b>Apollo AI Trader 日报</b>")
        lines.append(f"📅 {today}")
        lines.append("")

        # 查询选股数据
        try:
            conn = self._connect()
            try:
                df = pd.read_sql_query(
                    "SELECT code, name, score FROM stock_selections "
                    "WHERE date(timestamp) = date('now') "
                    "ORDER BY score DESC LIMIT 10",
                    conn
                )
                if not df.empty:
                    lines.append("🏆 <b>今日AI选股 Top 10</b>")
                    for i, row in df.iterrows():
                        code = row['code']
                        name = row.get('name', '') or ""
                        score = row['score']
                        lines.append(f"  {i+1}. {code} {name} | 评分 {score:.1f}")
                else:
                    lines.append("🏆 今日选股：暂无数据")
            finally:
                conn.close()
        except Exception as e:
            lines.append(f"⚠️ 选股数据读取失败: {e}")

        lines.append("")

        # 查询事件数据
        try:
            conn = self._connect()
            try:
                df = pd.read_sql_query(
                    "SELECT text FROM events "
                    "WHERE date(timestamp) = date('now') "
                    "ORDER BY id DESC LIMIT 10",
                    conn
                )
                if not df.empty:
                    lines.append("📋 <b>今日事件</b>")
                    for _, row in df.iterrows():
                        lines.append(f"  • {row['text']}")
                else:
                    lines.append("📋 今日事件：无")
            finally:
                conn.close()
        except Exception as e:
            lines.append(f"⚠️ 事件读取失败: {e}")

        lines.append("")
        lines.append(f"🕐 生成时间: {now}")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("⚠️ 本系统仅供模拟测试，不构成投资建议")

        return "\n".join(lines)

    def generate_weekly(self) -> str:
        """生成周报（简化版）"""
        return self.generate_daily()

    def generate_trade_summary(self, symbol: str = "") -> str:
        """生成单个标的的交易摘要"""
        return f"📈 {symbol} 交易摘要：功能开发中"