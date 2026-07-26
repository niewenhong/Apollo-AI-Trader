"""
core/strategy_matcher.py — 策略匹配器
基于Regime概率 + 策略绩效表，Softmax加权路由
"""
import math
import sqlite3
import logging

log = logging.getLogger("StrategyMatcher")

# 策略在各Regime下的历史Sharpe（可定期更新）
DEFAULT_SHARPE_TABLE = {
    "TrendStrategy":         {"trend": 2.0, "range": 0.3, "volatile": -0.2},
    "GridStrategy":          {"trend": 0.2, "range": 1.8, "volatile": 0.1},
    "OrderFlowStrategy":     {"trend": 0.5, "range": 0.4, "volatile": 2.3},
    "MultiIndicator":        {"trend": 1.2, "range": 1.0, "volatile": 0.8},
    "DualThrust":            {"trend": 1.5, "range": 0.6, "volatile": 0.4},
    "VWAPStrategy":          {"trend": 0.8, "range": 1.2, "volatile": 0.5},
}


class StrategyMatcher:
    """策略匹配器"""

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self.sharpe_table = DEFAULT_SHARPE_TABLE.copy()

    def update_sharpe(self, strategy: str, regime: str, sharpe: float):
        """更新策略绩效（从回测或实盘结果）"""
        if strategy in self.sharpe_table:
            self.sharpe_table[strategy][regime] = sharpe

    def get_weights(self, symbol: str, market: str = "US") -> dict:
        """根据Regime概率计算各策略权重"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""SELECT prob_trend, prob_range, prob_volatile FROM regime_records
            WHERE symbol=? AND exchange=? ORDER BY rowid DESC LIMIT 1""",
            (symbol, market))
        row = c.fetchone()
        conn.close()

        if not row:
            n = len(self.sharpe_table)
            return {s: 1.0/n for s in self.sharpe_table}

        prob = {"trend": row[0], "range": row[1], "volatile": row[2]}

        # 加权得分
        scores = {}
        for strat, regime_scores in self.sharpe_table.items():
            score = sum(prob[r] * regime_scores[r] for r in prob)
            scores[strat] = max(score, 0)

        # Softmax
        exp_s = {k: math.exp(v) for k, v in scores.items()}
        total = sum(exp_s.values())
        if total == 0:
            n = len(self.sharpe_table)
            return {s: 1.0/n for s in self.sharpe_table}

        return {k: round(v/total, 6) for k, v in exp_s.items()}

    def select_strategy(self, symbol: str, market: str = "US") -> str:
        """选择权重最高的策略"""
        weights = self.get_weights(symbol, market)
        return max(weights, key=weights.get)