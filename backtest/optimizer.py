"""
backtest/optimizer.py - v2.6.0
参数优化器（简化版，基于决策引擎打分）
"""
import itertools
import json
from datetime import datetime
from typing import Dict, List

from core.db_manager import CustomDBManager


class ParameterOptimizer:
    def __init__(self, db: CustomDBManager):
        self.db = db

    def optimize(self, strategy_class: str, param_grid: Dict[str, List],
                 vt_symbol: str = "", metric: str = "score",
                 n_trials: int = 50) -> Dict:
        keys = list(param_grid.keys())
        combos = list(itertools.product(*param_grid.values()))
        if len(combos) > n_trials:
            import random
            combos = random.sample(combos, n_trials)

        best_score = -1e9
        best_params = {}
        for combo in combos:
            params = dict(zip(keys, combo))
            score = self._score(strategy_class, vt_symbol, params, metric)
            if score > best_score:
                best_score = score
                best_params = params
        return best_params

    def _score(self, strategy_class, vt_symbol, params, metric) -> float:
        # 占位：实际项目里这里调 decision_engine.evaluate_params
        # 为避免循环导入，这里只做随机基线，保证不崩
        return hash((strategy_class, vt_symbol, json.dumps(params, sort_keys=True))) % 1000

    def get_best_params(self, strategy_class: str, vt_symbol: str = "") -> Dict:
        return {}