# -*- coding: utf-8 -*-
"""
周末维护脚本
- 回测所有策略
- AI 参数建议
- AI 选股
- 数据整理
- 推送报告到 Telegram
"""
import sys
import os
import json
import logging
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backtest.engine import BacktestEngine, Bar
from backtest.optimizer import ParamOptimizer
from backtest.data_loader import load_csv, load_database
from strategies.equity.vwap_strategy import VwapStrategy
from strategies.equity.triple_filter_scalp_strategy import TripleFilterScalpStrategy
from strategies.futures.momentum_strategy import FuturesMomentumStrategy
from strategies.equity.trend_strategy import TrendStrategy
from ai.llm_client import LLMClient, LLMConfig
from ai.stock_selector import StockSelector
from ai.param_advisor import ParamAdvisor
from ai.report_generator import ReportGenerator as AIReportGenerator
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.daily_report import DailyReportGenerator
from utils.logger import setup_logging

STRATEGIES = {
    "vwap": VwapStrategy,
    "triple_filter": TripleFilterScalpStrategy,
    "futures_momentum": FuturesMomentumStrategy,
    "trend": TrendStrategy,
}

DEFAULT_SPACES = {
    "vwap": {
        "threshold_long": [-3.0, -2.0, -1.5, -1.0],
        "threshold_short": [0.5, 1.0, 1.5, 2.0],
        "exit_band": [0.1, 0.3, 0.5],
    },
    "triple_filter": {
        "ema_fast": [10, 15, 20],
        "ema_slow": [40, 60, 80],
        "rsi_oversold": [20, 25, 30],
        "rsi_overbought": [75, 78, 80],
    },
    "futures_momentum": {
        "ema_fast": [10, 20, 30],
        "ema_slow": [40, 60, 80],
        "atr_filter_mult": [0.3, 0.5, 1.0],
    },
}


def main():
    setup_logging(os.path.join(ROOT, "config/logging_config.json"))
    logger = logging.getLogger("scripts.weekend")
    logger.info("=" * 60)
    logger.info("  Apollo AI Trader v2.2.0 — WEEKEND MAINTENANCE")
    logger.info(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    notifier = TelegramNotifier()
    notifier.send_message("🔧 周末维护开始...", "info")

    # ====== 1. 回测 + 参数优化 ======
    logger.info("\n📊 阶段 1: 回测与参数优化")
    all_results = {}

    for name, cls in STRATEGIES.items():
        data_path = os.path.join(ROOT, f"data/cache/{name}_data.csv")
        if not os.path.exists(data_path):
            # 尝试从数据库加载
            db_path = os.path.join(ROOT, "data/database/market_data.db")
            symbol_map = {"vwap": "NVDA", "triple_filter": "NVDA",
                         "futures_momentum": "ES", "trend": "NVDA"}
            sym = symbol_map.get(name, "NVDA")
            bars = load_database(db_path, sym)
        else:
            bars = load_csv(data_path)

        if not bars:
            logger.warning(f"[Weekend] {name}: 无数据，跳过")
            continue

        engine = BacktestEngine()
        space = DEFAULT_SPACES.get(name, {})

        # 网格搜索
        optimizer = ParamOptimizer(engine)
        if space:
            results = optimizer.grid_search(cls, bars, space)
            best_params = results[0].params if results else {}
            best_metrics = results[0].metrics if results else {}
            all_results[name] = {"params": best_params, "metrics": best_metrics}
            logger.info(f"  {name}: 最优收益={best_metrics.get('total_return_pct',0):+.2f}% "
                        f"胜率={best_metrics.get('win_rate_pct',0):.0f}%")

            # 保存最优参数
            config_path = os.path.join(ROOT, f"config/strategies/{name}_config.json")
            if os.path.exists(config_path) and best_params:
                with open(config_path, "r") as f:
                    current = json.load(f)
                current.update(best_params)
                with open(config_path, "w") as f:
                    json.dump(current, f, indent=4)
                logger.info(f"  {name}: 参数已更新")

    # ====== 2. AI 参数建议 ======
    logger.info("\n🤖 阶段 2: AI 参数建议")
    try:
        llm_config = LLMConfig(os.path.join(ROOT, "config/ai_config.json"))
        llm = LLMClient(llm_config)
        advisor = ParamAdvisor(llm)

        for name, data in all_results.items():
            advice = advisor.analyze([{"strategy": name, **data["metrics"]}])
            if advice:
                logger.info(f"  {name}: AI 建议已生成")
    except Exception as e:
        logger.error(f"[Weekend] AI 参数建议失败: {e}")

    # ====== 3. AI 选股 ======
    logger.info("\n📋 阶段 3: AI 选股")
    try:
        selector = StockSelector(llm)
        # 构造市场数据（简化版）
        market_data = {}
        for sym in ["NVDA", "AAPL", "MSFT", "TSLA", "GOOGL"]:
            # 从数据库读取最新指标
            # 这里用占位数据
            market_data[sym] = {
                "price": 0.0,
                "rsi": 50,
                "macd": 0.0,
                "ema20": 0.0,
                "ema60": 0.0,
                "volume_ratio": 1.0,
                "atr": 0.0
            }
        selections = selector.select(market_data, top_n=10)
        if selections:
            selector.apply_to_config(selections)
            notifier.send_message(
                f"📋 AI 选股完成: {len(selections)} 只\n"
                f"Top: {selections[0].get('symbol','')} "
                f"score={selections[0].get('score',0)}",
                "info"
            )
    except Exception as e:
        logger.error(f"[Weekend] AI 选股失败: {e}")

    # ====== 4. 生成周报 ======
    logger.info("\n📊 阶段 4: 生成报告")
    report_data = {
        "total_return_pct": sum(r["metrics"].get("total_return_pct", 0) for r in all_results.values()) / max(len(all_results), 1),
        "win_rate": sum(r["metrics"].get("win_rate_pct", 0) for r in all_results.values()) / max(len(all_results), 1),
        "max_drawdown_pct": max((r["metrics"].get("max_drawdown_pct", 0) for r in all_results.values()), default=0),
        "sharpe_ratio": sum(r["metrics"].get("sharpe_ratio", 0) for r in all_results.values()) / max(len(all_results), 1),
    }

    report_gen = DailyReportGenerator(os.path.join(ROOT, "data/export"))
    report = report_gen.generate(date_str=datetime.now().strftime("%Y-%m-%d"), data=report_data)
    report_gen.save_json(report)
    text_path = report_gen.save_text(report)
    logger.info(f"[Weekend] 报告已生成: {text_path}")

    # 推送到 Telegram
    notifier.send_message(
        f"📊 周末维护完成\n"
        f"平均收益: {report_data['total_return_pct']:+.2f}%\n"
        f"平均胜率: {report_data['win_rate']:.0f}%\n"
        f"最大回撤: {report_data['max_drawdown_pct']:.2f}%",
        "success"
    )

    logger.info("\n" + "=" * 60)
    logger.info("  ✅ 周末维护全部完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
