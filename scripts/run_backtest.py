# -*- coding: utf-8 -*-
"""
回测模式入口
- 加载历史数据
- 运行指定策略回测
- 生成报告
"""
import sys
import os
import json
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backtest.engine import BacktestEngine, Bar
from backtest.data_loader import load_csv, load_database, load_from_vnpy
from backtest.report import ReportGenerator
from strategies.equity.vwap_strategy import VwapStrategy
from strategies.equity.triple_filter_scalp_strategy import TripleFilterScalpStrategy
from strategies.futures.momentum_strategy import FuturesMomentumStrategy
from strategies.equity.trend_strategy import TrendStrategy
from utils.logger import setup_logging

# 策略映射
STRATEGIES = {
    "vwap": VwapStrategy,
    "triple_filter": TripleFilterScalpStrategy,
    "futures_momentum": FuturesMomentumStrategy,
    "trend": TrendStrategy,
}


def main():
    setup_logging(os.path.join(ROOT, "config/logging_config.json"))
    logger = logging.getLogger("scripts.run_backtest")
    logger.info("=" * 60)
    logger.info("  Apollo AI Trader v2.2.0 — BACKTEST MODE")
    logger.info("=" * 60)

    # 解析参数
    strategy_name = sys.argv[1] if len(sys.argv) > 1 else "vwap"
    data_path = sys.argv[2] if len(sys.argv) > 2 else "data/cache/NVDA_1m.csv"
    capital = float(sys.argv[3]) if len(sys.argv) > 3 else 100000.0

    if strategy_name not in STRATEGIES:
        logger.error(f"未知策略: {strategy_name}")
        logger.info(f"可用策略: {list(STRATEGIES.keys())}")
        sys.exit(1)

    # 加载数据
    if data_path.endswith(".csv"):
        bars = load_csv(data_path)
    elif data_path == "vnpy":
        symbol = sys.argv[4] if len(sys.argv) > 4 else "NVDA.SMART"
        from datetime import datetime
        start = datetime(2024, 1, 1)
        end = datetime(2024, 12, 31)
        bars = load_from_vnpy(symbol, start, end)
    else:
        bars = load_database(data_path, "NVDA")

    if not bars:
        logger.error("无数据，退出")
        sys.exit(1)

    logger.info(f"[Backtest] 策略={strategy_name} 数据={len(bars)}根K线 资金={capital}")

    # 加载策略参数
    config_path = os.path.join(ROOT, f"config/strategies/{strategy_name}_config.json")
    params = {}
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            params = json.load(f)
        logger.info(f"[Backtest] 参数已加载: {config_path}")

    # 运行回测
    engine = BacktestEngine(capital=capital)
    strategy_class = STRATEGIES[strategy_name]
    result = engine.run(strategy_class, bars, params, symbol=strategy_name)

    # 输出结果
    logger.info("=" * 40)
    logger.info(f"  总收益率:  {result['total_return_pct']:+.2f}%")
    logger.info(f"  胜率:      {result['win_rate_pct']:.1f}%")
    logger.info(f"  最大回撤:  {result['max_drawdown_pct']:.2f}%")
    logger.info(f"  盈亏比:    {result['profit_factor']:.2f}")
    logger.info(f"  交易次数:  {result['num_trades']}")
    logger.info(f"  夏普比率:  {result['sharpe_ratio']:.2f}")
    logger.info("=" * 40)

    # 生成报告
    report_gen = ReportGenerator(engine.results)
    report_gen.save_json(os.path.join(ROOT, "data/export/backtest_results.json"))
    report_gen.save_html(os.path.join(ROOT, "data/export/backtest_report.html"))
    logger.info("[Backtest] 报告已生成")

    # 保存最优参数
    best = engine.get_best_params()
    if best:
        best_path = os.path.join(ROOT, "data/cache/best_params.json")
        with open(best_path, "w") as f:
            json.dump(best, f, indent=4, default=str)
        logger.info(f"[Backtest] 最优参数已保存: {best_path}")


if __name__ == "__main__":
    main()
