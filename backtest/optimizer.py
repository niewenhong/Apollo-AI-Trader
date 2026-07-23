"""
backtest/optimizer.py - Apollo-AI-Trader v2.6.0
回测优化器：网格搜索 + Walk-forward 验证 + AI审核
"""
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.db_manager import CustomDBManager
from core.decision_engine import DecisionEngine


class ParamOptimizer:
    """参数优化器：对执行池标的执行网格搜索+Walk-forward验证"""

    def __init__(self, db: CustomDBManager, decision_engine: DecisionEngine,
                 backtest_engine_factory=None):
        self.db = db
        self.decision = decision_engine
        self._factory = backtest_engine_factory  # 注入回测引擎工厂

    def optimize(self, vt_symbol: str, strategy_class_name: str,
                 param_grid: dict, current_params: dict) -> Optional[dict]:
        """执行完整优化流程：网格搜索→Walk-forward→AI审核"""
        # 1. 网格搜索
        best = self._grid_search(vt_symbol, strategy_class_name, param_grid)
        if not best:
            print(f"[Optimize] {vt_symbol} 无有效回测结果")
            return None
        best_params, best_stats = best
        # 2. Walk-forward 验证（用最近3个月数据）
        val_stats = self._walk_forward(vt_symbol, strategy_class_name, best_params)
        if val_stats:
            best_stats.update({"val_sharpe": val_stats.get("sharpe",0),
                              "val_max_dd": val_stats.get("max_dd",1)})
        # 3. 保存回测结果
        self.db.save_backtest_result(vt_symbol, strategy_class_name,
                                      best_params, best_stats, validated=bool(val_stats))
        # 4. AI审核
        old_stats = self._get_old_stats(vt_symbol, strategy_class_name, current_params)
        decision = self.decision.review(vt_symbol, best_params, best_stats, old_stats)
        # 5. 如果通过，保存参数建议
        if decision == "accept":
            self.db.save_param_suggestion(vt_symbol, strategy_class_name,
                                           best_params, source="auto_optimize", conf=0.8)
            print(f"[Optimize] ✅ {vt_symbol} 参数通过审核")
            return best_params
        else:
            print(f"[Optimize] ⏸ {vt_symbol} 参数{direction}: {decision}")
            return None

    def _grid_search(self, vt_symbol, sclass, grid) -> Optional[Tuple[dict, dict]]:
        """遍历参数组合（简化版：顺序执行，可改为并行）"""
        from itertools import product
        import numpy as np
        keys, values = zip(*grid.items()) if isinstance(grid, dict) else ([],[])
        best_score = -float('inf'); best_params = None; best_stats = None
        for combo in product(*values):
            params = dict(zip(keys, combo))
            try:
                stats = self._run_backtest(vt_symbol, sclass, params)
                if not stats: continue
                score = stats.get("sharpe",0)*0.5 + stats.get("annual",0)*0.3 \
                        - stats.get("max_dd",0)*0.2
                if score > best_score:
                    best_score = score; best_params = params; best_stats = stats
            except Exception as e:
                print(f"[Grid] {params} fail: {e}")
        return (best_params, best_stats) if best_params else None

    def _walk_forward(self, vt_symbol, sclass, params) -> Optional[dict]:
        """用最近3个月数据验证"""
        # 实际实现需调用回测引擎加载近3个月数据
        # 此处返回None表示跳过验证
        return None

    def _run_backtest(self, vt_symbol, sclass, params) -> Optional[dict]:
        """单次回测（需注入回测引擎）"""
        if self._factory:
            engine = self._factory(vt_symbol)
            return engine.run(params)
        return None

    def _get_old_stats(self, vt_symbol, sclass, params) -> dict:
        """获取当前参数的表现（用于对比）"""
        return {"sharpe": 0.0, "max_dd": 0.3, "annual": 0.0}
