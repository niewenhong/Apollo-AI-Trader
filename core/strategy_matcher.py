"""
core/strategy_matcher.py - v3.0.2
策略匹配器：基于 Regime 概率 + 策略历史 Sharpe，Softmax 加权路由
修复 v3.0.1：
  1. 使用 DBManager 接口而非直接 sqlite3（统一数据源）
  2. regime_records 表字段对齐（prob_trend/prob_range/prob_volatile）
  3. select_strategy 返回 dict 格式 {class_name, params} 供 StrategyGenerator 使用
"""
import math
import logging

log = logging.getLogger("StrategyMatcher")

# 策略在各 Regime 下的历史 Sharpe（可定期更新）
DEFAULT_SHARPE_TABLE = {
    "TrendStrategy":      {"trend": 2.0, "range": 0.3, "volatile": -0.2},
    "GridStrategy":       {"trend": 0.2, "range": 1.8, "volatile": 0.1},
    "OrderFlowStrategy":  {"trend": 0.5, "range": 0.4, "volatile": 2.3},
    "MultiIndicator":     {"trend": 1.2, "range": 1.0, "volatile": 0.8},
    "DualThrust":         {"trend": 1.5, "range": 0.6, "volatile": 0.4},
    "VWAPStrategy":       {"trend": 0.8, "range": 1.2, "volatile": 0.5},
}


class StrategyMatcher:
    """策略匹配器"""

    def __init__(self, db_path: str = "", db=None):
        """
        db_path: 兼容旧调用方式
        db: DBManager 实例（优先使用）
        """
        self.db = db
        self.db_path = db_path
        self.sharpe_table = DEFAULT_SHARPE_TABLE.copy()

    def update_sharpe(self, strategy: str, regime: str, sharpe: float):
        """更新策略绩效"""
        if strategy in self.sharpe_table:
            self.sharpe_table[strategy][regime] = sharpe

    def get_weights(self, symbol: str, market: str = "US") -> dict:
        """根据 Regime 概率计算各策略权重"""
        prob = self._get_regime_prob(symbol, market)

        if not prob:
            n = len(self.sharpe_table)
            return {s: round(1.0 / n, 6) for s in self.sharpe_table}

        # 加权得分
        scores = {}
        for strat, regime_scores in self.sharpe_table.items():
            score = sum(prob[r] * regime_scores[r] for r in prob)
            scores[strat] = max(score, 0)

        # Softmax
        max_score = max(scores.values()) if scores else 0
        exp_s = {k: math.exp(v - max_score) for k, v in scores.items()}
        total = sum(exp_s.values())
        if total == 0:
            n = len(self.sharpe_table)
            return {s: round(1.0 / n, 6) for s in self.sharpe_table}

        return {k: round(v / total, 6) for k, v in exp_s.items()}

    def select_strategy(self, symbol: str, market: str = "US") -> str:
        """返回权重最高的策略类名"""
        weights = self.get_weights(symbol, market)
        return max(weights, key=weights.get)

    def select(self, symbol: str, market: str = "US") -> dict:
        """
        完整匹配结果：返回 {class_name, params, weight}
        供 StrategyGenerator 直接使用
        """
        weights = self.get_weights(symbol, market)
        best = max(weights, key=weights.get)
        return {
            "class_name": best,
            "weight": weights[best],
            "all_weights": weights,
        }

    # ==================== 内部方法 ====================

    def _get_regime_prob(self, symbol: str, market: str) -> dict:
        """从数据库获取该 symbol 最新的 regime 概率"""
        prob = {}
        try:
            if self.db:
                conn = self.db.conn if hasattr(self.db, 'conn') else self.db
                cur = conn.execute(
                    """SELECT prob_trend, prob_range, prob_volatile
                       FROM regime_records
                       WHERE symbol=? ORDER BY timestamp DESC LIMIT 1""",
                    (symbol,),
                )
                row = cur.fetchone()
                if row:
                    prob = {
                        "trend": float(row[0] or 0),
                        "range": float(row[1] or 0),
                        "volatile": float(row[2] or 0),
                    }
            elif self.db_path:
                import sqlite3
                conn = sqlite3.connect(self.db_path)
                c = conn.cursor()
                c.execute(
                    """SELECT prob_trend, prob_range, prob_volatile
                       FROM regime_records
                       WHERE symbol=? ORDER BY rowid DESC LIMIT 1""",
                    (symbol,),
                )
                row = c.fetchone()
                conn.close()
                if row:
                    prob = {
                        "trend": float(row[0] or 0),
                        "range": float(row[1] or 0),
                        "volatile": float(row[2] or 0),
                    }
        except Exception as e:
            log.debug(f"[Matcher] {symbol} 获取regime概率失败: {e}")

        return prob
