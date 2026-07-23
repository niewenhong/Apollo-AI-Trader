# -*- coding: utf-8 -*-
"""
回测报告生成（HTML + JSON）
"""
import json
import os
import logging
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger("backtest.report")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Apollo Backtest Report</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 40px; background: #1a1a2e; color: #eee; }
h1 { color: #e94560; }
table { border-collapse: collapse; width: 100%; margin-top: 20px; }
th { background: #16213e; color: #e94560; padding: 10px; text-align: left; }
td { padding: 8px 10px; border-bottom: 1px solid #333; }
tr:hover { background: #16213e; }
.metric { display: inline-block; margin: 10px 20px 10px 0; padding: 15px; background: #16213e; border-radius: 8px; }
.metric-value { font-size: 24px; font-weight: bold; color: #e94560; }
.metric-label { font-size: 12px; color: #aaa; }
.win { color: #0f0; }
.loss { color: #f00; }
</style>
</head>
<body>
<h1>🚀 Apollo Backtest Report</h1>
<p>生成时间: {timestamp}</p>
<h2>📊 绩效摘要</h2>
<div class="metric"><div class="metric-value">{total_return}%</div><div class="metric-label">总收益率</div></div>
<div class="metric"><div class="metric-value {win_class}">{win_rate}%</div><div class="metric-label">胜率</div></div>
<div class="metric"><div class="metric-value {dd_class}">{max_dd}%</div><div class="metric-label">最大回撤</div></div>
<div class="metric"><div class="metric-value">{sharpe}</div><div class="metric-label">夏普比率</div></div>
<div class="metric"><div class="metric-value">{pf}</div><div class="metric-label">盈亏比</div></div>
<div class="metric"><div class="metric-value">{trades}</div><div class="metric-label">交易次数</div></div>
<h2>📋 最优参数</h2>
<pre>{params_json}</pre>
<h2>📈 全部结果</h2>
<table>
<tr><th>排名</th><th>策略</th><th>收益率%</th><th>胜率%</th><th>回撤%</th><th>夏普</th><th>盈亏比</th><th>交易数</th></tr>
{rows}
</table>
</body>
</html>"""


class ReportGenerator:
    """回测报告生成器"""

    def __init__(self, results: List[dict]):
        self.results = results

    def generate_html(self, best_params: dict = None) -> str:
        """生成 HTML 报告"""
        if not self.results:
            return "<h1>No results</h1>"

        best = self.results[0]
        win_class = "win" if best.get("win_rate_pct", 0) >= 50 else "loss"
        dd_class = "loss" if best.get("max_drawdown_pct", 0) > 10 else "win"

        rows = ""
        for r in self.results[:50]:
            cls = "win" if r.get("total_return_pct", 0) > 0 else "loss"
            rows += (
                f"<tr class='{cls}'>"
                f"<td>{r.get('rank', '')}</td>"
                f"<td>{r.get('strategy', '')}</td>"
                f"<td>{r.get('total_return_pct', 0)}</td>"
                f"<td>{r.get('win_rate_pct', 0)}</td>"
                f"<td>{r.get('max_drawdown_pct', 0)}</td>"
                f"<td>{r.get('sharpe_ratio', 0)}</td>"
                f"<td>{r.get('profit_factor', 0)}</td>"
                f"<td>{r.get('num_trades', 0)}</td>"
                f"</tr>\n"
            )

        return HTML_TEMPLATE.format(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            total_return=best.get("total_return_pct", 0),
            win_rate=best.get("win_rate_pct", 0),
            max_dd=best.get("max_drawdown_pct", 0),
            sharpe=best.get("sharpe_ratio", 0),
            pf=best.get("profit_factor", 0),
            trades=best.get("num_trades", 0),
            win_class=win_class,
            dd_class=dd_class,
            params_json=json.dumps(best_params or best.get("params", {}), indent=2, ensure_ascii=False),
            rows=rows
        )

    def save_html(self, filepath: str = "data/export/backtest_report.html"):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        html = self.generate_html()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"[Report] HTML 报告已保存: {filepath}")

    def save_json(self, filepath: str = "data/export/backtest_results.json"):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=4, default=str, ensure_ascii=False)
        logger.info(f"[Report] JSON 结果已保存: {filepath}")
