"""
ai/report_generator.py - Apollo Trader v2.6.0
报告生成：根据数据库中的选股/诊股/回测结果生成HTML报告
"""
import json
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path
from core.db_manager import CustomDBManager


class ReportGenerator:
    """生成HTML格式的投资报告"""

    def __init__(self, db: CustomDBManager, output_dir: str = "reports"):
        self.db = db
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_daily_report(self, market: str = "US") -> str:
        """生成每日投资报告"""
        # 获取选股池
        pool = self.db.get_top_pool(n=30, market=market)
        # 获取执行池
        exec_pool = self.db.get_pool(market=market)

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>Apollo AI Trader 日报</title>
<style>
body {{ font-family: 'Segoe UI', sans-serif; margin: 20px; background: #f5f5f5; }}
h1 {{ color: #1a73e8; }}
table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
th {{ background: #1a73e8; color: white; padding: 10px; text-align: left; }}
td {{ padding: 8px; border-bottom: 1px solid #ddd; }}
tr:hover {{ background: #f0f8ff; }}
.green {{ color: #2e7d32; }}
.red {{ color: #c62828; }}
.header {{ display: flex; justify-content: space-between; align-items: center; }}
</style></head>
<body>
<div class="header">
<h1>📊 Apollo AI Trader 日报</h1>
<span>{datetime.now().strftime('%Y-%m-%d %H:%M')}</span>
</div>
<h2>🏆 AI精选标的 Top 30</h2>
<table>
<tr><th>排名</th><th>标的</th><th>评分</th><th>理由</th></tr>
"""
        for i, s in enumerate(pool[:30], 1):
            html += f"<tr><td>{i}</td><td>{s['vt_symbol']}</td><td>{s['score']}</td><td>{s.get('reason','')}</td></tr>"
        html += "</table>"

        html += "<h2>⚙️ 当前执行池</h2><table><tr><th>标的</th><th>策略</th><th>状态</th></tr>"
        for e in exec_pool:
            html += f"<tr><td>{e['vt_symbol']}</td><td>{e['strategy_class']}</td><td>{e['status']}</td></tr>"
        html += "</table>"

        html += "<h2>📈 回测表现最佳参数</h2><table><tr><th>标的</th><th>策略</th><th>夏普比率</th><th>年化收益</th></tr>"
        # 从回测结果表中获取前10条
        rows = self.db.conn.execute(
            "SELECT vt_symbol, strategy_class, metrics_json FROM backtest_results ORDER BY json_extract(metrics_json,'$.sharpe_ratio') DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            m = json.loads(r["metrics_json"])
            html += f"<tr><td>{r['vt_symbol']}</td><td>{r['strategy_class']}</td><td class='green'>{m.get('sharpe_ratio',0):.2f}</td><td>{m.get('annual_return',0)*100:.1f}%</td></tr>"
        html += "</table>"

        html += "</body></html>"
        filename = f"daily_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        filepath = self.output_dir / filename
        filepath.write_text(html, encoding="utf-8")
        print(f"[Report] 日报已生成: {filepath}")
        return str(filepath)

    def generate_diagnosis_report(self, vt_symbol: str) -> str:
        """生成单票诊断报告"""
        diag = self.db.get_latest_diagnosis(vt_symbol)
        if not diag:
            return ""

        d = json.loads(diag["diagnosis_json"])
        tech = d.get("technical", {})
        mf = d.get("money_flow", {})
        trend = d.get("trend", {})

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>{vt_symbol} 诊股报告</title>
<style>
body {{ font-family: 'Segoe UI', sans-serif; margin: 20px; }}
.card {{ background: white; border-radius: 8px; padding: 20px; margin: 10px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
h2 {{ color: #1a73e8; }}
.value {{ font-size: 24px; font-weight: bold; }}
</style></head>
<body>
<h1>🔍 {vt_symbol} 诊股报告</h1>
<p>生成时间: {diag['created_at']}</p>
<div class="card"><h2>技术面</h2>
<p>收盘价: <span class="value">{tech.get('close','N/A')}</span></p>
<p>MA5: {tech.get('ma5','N/A')} | MA20: {tech.get('ma20','N/A')} | MA60: {tech.get('ma60','N/A')}</p>
<p>RSI(14): {tech.get('rsi14','N/A')}</p>
<p>均线排列: {tech.get('arrangement','N/A')}</p>
<p>距离MA20: {tech.get('dist_from_ma20_pct','N/A'):.2f}%</p>
</div>
<div class="card"><h2>资金面</h2>
<p>净流入: {mf.get('net_inflow','N/A')}</p>
<p>方向: {mf.get('direction','N/A')}</p>
</div>
<div class="card"><h2>趋势</h2>
<p>52周高位: {trend.get('high_52w','N/A')} | 52周低位: {trend.get('low_52w','N/A')}</p>
<p>52周位置: {trend.get('position_52w_pct',0)*100:.1f}%</p>
<p>周线趋势: {trend.get('week_trend','N/A')}</p>
</div>
<p><strong>总结:</strong> {diag.get('summary','')}</p>
</body></html>"""
        filename = f"diagnosis_{vt_symbol.replace('.','_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        filepath = self.output_dir / filename
        filepath.write_text(html, encoding="utf-8")
        return str(filepath)