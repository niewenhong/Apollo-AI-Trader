"""
core/param_optimizer.py — 参数优化器
Optuna贝叶斯优化 + 版本管理
"""
import json
import sqlite3
import logging
from datetime import datetime

log = logging.getLogger("ParamOptimizer")

try:
    import optuna
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False
    log.warning("Optuna未安装，参数优化不可用")


class ParamOptimizer:
    """参数优化器"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.db_path = self.config.get("database", {}).get("path", "trading.db")

    def optimize(self, symbol: str, strategy_name: str,
                 n_trials: int = 20, db_path: str = None) -> tuple:
        """执行参数优化"""
        db = db_path or self.db_path

        if not OPTUNA_AVAILABLE:
            log.warning("Optuna不可用，跳过优化")
            return {}, 0.0

        def objective(trial):
            fast_ma = trial.suggest_int("fast_ma", 5, 30)
            slow_ma = trial.suggest_int("slow_ma", 20, 60)
            stop_loss = trial.suggest_float("stop_loss_pct", 0.02, 0.08)
            take_profit = trial.suggest_float("take_profit_pct", 0.05, 0.20)

            # 模拟回测（实际应调用策略引擎）
            import random
            sharpe = random.gauss(0.5, 0.3)
            sharpe += 0.1 * (fast_ma / 30) - 0.05 * (slow_ma / 60)
            return sharpe

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=n_trials)

        best_params = dict(study.best_params)
        best_value = float(study.best_value)

        # 存储
        conn = sqlite3.connect(db)
        conn.execute("""INSERT OR REPLACE INTO param_optimization_results
            (symbol, strategy_name, regime, params_json, performance_json, version, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (symbol, strategy_name, "all", json.dumps(best_params),
             json.dumps({"sharpe": best_value}), 1, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        log.info(f"优化完成: {symbol}/{strategy_name} Sharpe={best_value:.3f}")
        return best_params, best_value

    def get_best_params(self, symbol: str, strategy_name: str, db_path: str = None) -> dict:
        """读取最优参数"""
        db = db_path or self.db_path
        conn = sqlite3.connect(db)
        c = conn.cursor()
        c.execute("""SELECT params_json FROM param_optimization_results
            WHERE symbol=? AND strategy_name=? ORDER BY version DESC LIMIT 1""",
            (symbol, strategy_name))
        row = c.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
        return {}