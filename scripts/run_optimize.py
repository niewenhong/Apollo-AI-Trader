# -*- coding: utf-8 -*-
"""
参数优化入口
- 网格搜索 / 贝叶斯 / 遗传算法
- 输出 Top N 参数组合
"""
import sys
import os
import json
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backtest.engine import BacktestEngine, Bar
from backtest.optimizer import ParamOptimizer
from backtest.data_loader import load_csv
from strategies.equity.vwap_strategy import VwapStrategy
from strategies.equity.triple_filter_scalp_strategy import TripleFilterScalpStrategy
from strategies.futures.momentum_strategy import FuturesMomentumStrategy
from utils.logger import setup_logging

STRATEGIES = {
    "vwap": VwapStrategy,
    "triple_filter": TripleFilterScalpStrategy,
    "futures_momentum": FuturesMomentumStrategy,
}

# 各策略的默认搜索空间
DEFAULT_SPACES = {
    "vwap": {
        "threshold_long": [-3.0, -2.0, -1.5, -1.0, -0.5],
        "threshold_short": [0.5, 1.0, 1.5, 2.0, 3.0],
        "exit_band": [0.1, 0.3, 0.5, 1.0],
        "vol_rank_limit": [0.5, 0.8, 1.0, 1.5],
    },
    "triple_filter": {
        "ema_fast": [10, 15, 20, 25],
        "ema_slow": [40, 50, 60, 80],
        "rsi_period": [5, 6, 9, 14],
        "rsi_oversold": [20, 25, 30],
        "rsi_overbought": [70, 75, 78, 80],
    },
    "futures_momentum": {
        "ema_fast": [10, 15, 20, 30],
        "ema_slow": [40, 60, 80, 100],
        "atr_period": [10, 14, 20],
        "atr_filter_mult": [0.3, 0.5, 1.0],
        "trailing_stop_atr": [1.5, 2.0, 2.5, 3.0],
    },
}


def main():
    setup_logging(os.path.join(ROOT, "config/logging_config.json"))
    logger = logging.getLogger("scripts.run_optimize")
    logger.info("=" * 60)
    logger.info("  Apollo AI Trader v2.2.0 — OPTIMIZE MODE")
    logger.info("=" * 60)

    strategy_name = sys.argv[1] if len(sys.argv) > 1 else "vwap"
    data_path = sys.argv[2] if len(sys.argv) > 2 else "data/cache/NVDA_1m.csv"
    method = sys.argv[3] if len(sys.argv) > 3 else "grid"  # grid / bayes / genetic

    if strategy_name not in STRATEGIES:
        logger.error(f"未知策略: {strategy_name}")
        sys.exit(1)

    # 加载数据
    bars = load_csv(data_path)
    if not bars:
        logger.error(f"无法加载数据: {data_path}")
        sys.exit(1)

    param_space = DEFAULT_SPACES.get(strategy_name, {})
    if not param_space:
        logger.error(f"无默认搜索空间: {strategy_name}")
        sys.exit(1)

    # 运行优化
    engine = BacktestEngine()
    optimizer = ParamOptimizer(engine)
    strategy_class = STRATEGIES[strategy_name]

    logger.info(f"[Optimize] 策略={strategy_name} 方法={method} 数据={len(bars)}根")

    if method == "grid":
        results = optimizer.grid_search(strategy_class, bars, param_space)
    elif method == "bayes":
        # 转为 tuple 格式
        bayes_space = {k: (min(v), max(v)) for k, v in param_space.items()}
        results = optimizer.bayesian_optimize(strategy_class, bars, bayes_space, n_calls=50)
    elif method == "genetic":
        bayes_space = {k: (min(v), max(v)) for k, v in param_space.items()}
        results = optimizer.genetic_optimize(strategy_class, bars, bayes_space)
    else:
        logger.error(f"未知方法: {method}")
        sys.exit(1)

    # 输出 Top 10
    logger.info("=" * 50)
    logger.info("  TOP 10 参数组合")
    logger.info("=" * 50)
    for r in results[:10]:
        params_str = ", ".join(f"{k}={v}" for k, v in r.params.items())
        logger.info(f"  #{r.rank} 收益={r.metrics.get('total_return_pct',0):+.2f}% "
                    f"胜率={r.metrics.get('win_rate_pct',0):.0f}% "
                    f"回撤={r.metrics.get('max_drawdown_pct',0):.2f}%")
        logger.info(f"         {params_str}")

    # 保存
    out_path = os.path.join(ROOT, f"data/export/optimization_{strategy_name}_{method}.json")
    optimizer.save_results(out_path)

    # 将最优参数写入策略配置
    if results:
        best = results[0].params
        config_path = os.path.join(ROOT, f"config/strategies/{strategy_name}_config.json")
        with open(config_path, "r") as f:
            current = json.load(f)
        current.update(best)
        with open(config_path, "w") as f:
            json.dump(current, f, indent=4)
        logger.info(f"[Optimize] 最优参数已写入: {config_path}")


if __name__ == "__main__":
    main()
