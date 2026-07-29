"""
core/param_optimizer.py - v2.8.4 兼容版
参数优化器：Optuna贝叶斯优化 + 版本管理

关键事实：
  - DBManager 有 save_optimization_result() 方法
  - param_optimization_results 表字段：
    symbol, strategy_name, regime, params_json, performance_json, version, created_at
  - 修复原版 typo：trial.suggest → trial.suggest_*
"""
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger("ParamOptimizer")

try:
    import optuna
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False
    log.warning("Optuna未安装，参数优化不可用。pip install optuna")


class ParamOptimizer:
    """参数优化器"""

    def __init__(self, config: dict = None, db=None):
        """
        :param config: 全局配置 dict
        :param db: DBManager 实例（优先使用）
        """
        self.config = config or {}
        self.db = db
        self.db_path = self.config.get("database", {}).get("path", "data/history.db")

    def optimize(self, symbol: str, strategy_name: str,
                 n_trials: int = 20, db_path: str = None) -> tuple:
        """
        执行参数优化
        返回 (best_params_dict, best_sharpe_float)
        """
        db = db_path or self.db_path

        if not OPTUNA_AVAILABLE:
            log.warning("Optuna不可用，跳过优化")
            return {}, 0.0

        def objective(trial):
            # 修复：trial.suggest_int / suggest_float（原版 typo: suggest）
            fast_ma = trial.suggest_int("fast_ma", 5, 30)
            slow_ma = trial.suggest_int("slow_ma", 20, 60)
            stop_loss = trial.suggest_float("stop_loss_pct", 0.02, 0.08)
            take_profit = trial.suggest_float("take_profit_pct", 0.05, 0.20)

            # 模拟回测目标函数
            import random
            sharpe = random.gauss(0.5, 0.3)
            sharpe += 0.1 * (fast_ma / 30) - 0.05 * (slow_ma / 60)
            return sharpe

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=n_trials)

        best_params = dict(study.best_params)
        best_value = float(study.best_value)

        # 存储：优先用 DBManager，否则直接写 sqlite
        if self.db and hasattr(self.db, 'save_optimization_result'):
            try:
                self.db.save_optimization_result(
                    symbol=symbol,
                    strategy_name=strategy_name,
                    params=best_params,
                    performance={"sharpe": best_value},
                    regime="all",
                    version=1,
                )
            except Exception as e:
                log.warning(f"DBManager 保存失败，回退到直接写库: {e}")
                self._write_direct(db, symbol, strategy_name, best_params, best_value)
        else:
            self._write_direct(db, symbol, strategy_name, best_params, best_value)

        log.info(f"优化完成: {symbol}/{strategy_name} Sharpe={best_value:.3f}")
        return best_params, best_value

    def _write_direct(self, db_path: str, symbol: str, strategy_name: str,
                      best_params: dict, best_value: float):
        """直接写 sqlite（当 DBManager 不可用时）"""
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS param_optimization_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    regime TEXT DEFAULT 'all',
                    params_json TEXT DEFAULT '{}',
                    performance_json TEXT DEFAULT '{}',
                    version INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            conn.execute(
                """INSERT OR REPLACE INTO param_optimization_results
                   (symbol, strategy_name, regime, params_json, performance_json, version, created_at)
                   VALUES (?, ?, 'all', ?, ?, 1, ?)""",
                (symbol, strategy_name,
                 json.dumps(best_params, ensure_ascii=False),
                 json.dumps({"sharpe": best_value}),
                 datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"直接写库也失败: {e}")

    def get_best_params(self, symbol: str, strategy_name: str,
                        db_path: str = None) -> dict:
        """读取最优参数"""
        # 优先用 DBManager
        if self.db and hasattr(self.db, 'get_optimization_result'):
            try:
                result = self.db.get_optimization_result(symbol, strategy_name)
                if result:
                    return result
            except Exception:
                pass

        # 回退：直接读 sqlite
        db = db_path or self.db_path
        try:
            conn = sqlite3.connect(db)
            c = conn.cursor()
            c.execute(
                """SELECT params_json FROM param_optimization_results
                   WHERE symbol=? AND strategy_name=?
                   ORDER BY version DESC LIMIT 1""",
                (symbol, strategy_name)
            )
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                return json.loads(row[0])
        except Exception:
            pass
        return {}
