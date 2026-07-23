# -*- coding: utf-8 -*-
"""
每日报告生成器
汇总当日交易、绩效、系统状态，生成报告并推送
"""
import json
import os
import logging
from typing import Dict, Optional
from datetime import datetime, date
from collections import defaultdict

logger = logging.getLogger("monitoring.daily_report")


class DailyReportGenerator:
    """每日报告"""

    def __init__(self, output_dir: str = "data/export"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate(self, date_str: str = None, data: dict = None) -> dict:
        """
        生成每日报告
        :param date_str: 日期字符串 YYYY-MM-DD，默认昨天
        :param data: 报告数据（由引擎/数据库提供）
        :return: 报告字典
        """
        if not date_str:
            yesterday = date.today().toordinal() - 1
            date_str = date.fromordinal(yesterday).strftime("%Y-%m-%d")

        report = {
            "date": date_str,
            "generated_at": datetime.now().isoformat(),
            "equity": data.get("equity", 0) if data else 0,
            "daily_return_pct": data.get("daily_return_pct", 0) if data else 0,
            "total_return_pct": data.get("total_return_pct", 0) if data else 0,
            "positions_count": data.get("positions_count", 0) if data else 0,
            "trades_today": data.get("trades_today", 0) if data else 0,
            "win_rate": data.get("win_rate", 0) if data else 0,
            "max_drawdown_pct": data.get("max_drawdown_pct", 0) if data else 0,
            "sharpe_ratio": data.get("sharpe_ratio", 0) if data else 0,
            "profit_factor": data.get("profit_factor", 0) if data else 0,
            "top_strategies": data.get("top_strategies", []) if data else [],
            "top_trades": data.get("top_trades", []) if data else [],
            "system_health": data.get("system_health", {}) if data else {},
        }

        logger.info(f"[Report] 生成日报: {date_str} "
                    f"收益={report['daily_return_pct']:.2f}% "
                    f"交易={report['trades_today']}笔")
        return report

    def save_json(self, report: dict, filepath: str = None) -> str:
        """保存为 JSON"""
        if not filepath:
            filepath = os.path.join(self.output_dir, f"daily_report_{report['date']}.json")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=4, ensure_ascii=False)
        logger.info(f"[Report] JSON 已保存: {filepath}")
        return filepath

    def save_text(self, report: dict, filepath: str = None) -> str:
        """保存为文本格式"""
        if not filepath:
            filepath = os.path.join(self.output_dir, f"daily_report_{report['date']}.txt")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        text = (
            f"📊 Apollo 每日报告\n"
            f"日期: {report['date']}\n"
            f"生成时间: {report['generated_at']}\n"
            f"{'='*40}\n"
            f"账户净值: {report['equity']:.2f}\n"
            f"日收益率: {report['daily_return_pct']:+.2f}%\n"
            f"累计收益: {report['total_return_pct']:+.2f}%\n"
            f"今日交易: {report['trades_today']} 笔\n"
            f"胜率: {report['win_rate']:.1f}%\n"
            f"最大回撤: {report['max_drawdown_pct']:.2f}%\n"
            f"夏普比率: {report['sharpe_ratio']:.2f}\n"
            f"盈亏比: {report['profit_factor']:.2f}\n"
            f"持仓数: {report['positions_count']}\n"
        )
        if report.get("top_strategies"):
            text += f"\n📋 策略排名:\n"
            for s in report["top_strategies"][:5]:
                text += f"  {s.get('name','')}: {s.get('return_pct',0):+.2f}%\n"
        if report.get("system_health"):
            sh = report["system_health"]
            text += f"\n💻 系统状态:\n"
            text += f"  CPU: {sh.get('cpu_percent',0):.1f}%\n"
            text += f"  内存: {sh.get('memory_percent',0):.1f}%\n"
            text += f"  磁盘: {sh.get('disk_percent',0):.1f}%\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"[Report] 文本已保存: {filepath}")
        return filepath

    def format_for_telegram(self, report: dict) -> str:
        """格式化为 Telegram 消息"""
        return (
            f"📊 {report['date']} 每日报告\n"
            f"净值: {report['equity']:.2f}\n"
            f"日收益: {report['daily_return_pct']:+.2f}%\n"
            f"交易: {report['trades_today']}笔 "
            f"胜率: {report['win_rate']:.0f}%\n"
            f"回撤: {report['max_drawdown_pct']:.2f}%\n"
            f"夏普: {report['sharpe_ratio']:.2f}"
        )
