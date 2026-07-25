"""
strategy_matcher.py — 策略-标的匹配器 v2.7.0
- 输入: AI选股结果 + 可用策略库
- 输出: 每个标的的最优(策略+参数)组合
- 轻量评估: 用本地库最近数据快速回测
"""

import logging
from core.multi_period_db import MultiPeriodDB

logger = logging.getLogger(__name__)


class StrategyMatcher:
    def __init__(self, db: MultiPeriodDB, optimizer=None):
        self.db = db
        self.optimizer = optimizer
        self.param_templates = {
            ("MultiInd", "trend"):  {"fast": 10, "slow": 30, "atr_period": 14, "gate_multiplier": 3.0},
            ("MultiInd", "range"):  {"fast": 5,  "slow": 20, "atr_period": 10, "gate_multiplier": 2.0},
            ("MultiInd", "medium"): {"fast": 8,  "slow": 25, "atr_period": 14, "gate_multiplier": 2.5},
            ("DualThrust", "trend"):  {"n": 20, "k1": 0.4, "k2": 0.4},
            ("DualThrust", "range"):  {"n": 10, "k1": 0.6, "k2": 0.6},
        }

    def detect_regime(self, symbol):
        """检测当前市场regime"""
        bars = self.db.load_bars(symbol, "15m", limit=100)
        if len(bars) < 30:
            return "medium"
        closes = [b[5] for b in bars[-30:]]
        if not closes:
            return "medium"
        avg = sum(closes) / len(closes)
        volatility = (max(closes) - min(closes)) / avg if avg > 0 else 0
        trend = (closes[-1] - closes[0]) / avg if avg > 0 else 0
        if abs(trend) > 0.03:
            return "trend"
        elif volatility < 0.01:
            return "range"
        return "medium"

    def match(self, symbols, regime_override=None):
        results = []
        for sym in symbols:
            regime = regime_override or self.detect_regime(sym)
            best = self._match_one(sym, regime)
            if best:
                results.append(best)
        return sorted(results, key=lambda x: x["score"], reverse=True)

    def _match_one(self, symbol, regime):
        best_score = -999
        best_combo = None
        strategies = ["MultiInd", "DualThrust"]
        for sname in strategies:
            params = self.param_templates.get((sname, regime))
            if not params:
                params = self.param_templates.get((sname, "medium"))
            if not params:
                continue
            score = self._quick_eval(symbol, sname, params)
            if score > best_score:
                best_score = score
                best_combo = {"symbol": symbol, "strategy": sname,
                              "params": params, "score": score, "regime": regime}
        return best_combo

    def _quick_eval(self, symbol, sname, params):
        """用5m最近200根BAR做轻量评估"""
        bars = self.db.load_bars(symbol, "5m", limit=200)
        if len(bars) < 50:
            return -999
        closes = [b[5] for b in bars[-50:]]
        if not closes or closes[0] == 0:
            return -999

        avg = sum(closes) / len(closes)
        volatility = (max(closes) - min(closes)) / avg
        trend = (closes[-1] - closes[0]) / closes[0]
        win_rate_proxy = min(abs(trend) / (volatility + 0.001), 5.0)
        score = trend * win_rate_proxy / (volatility + 0.01)
        return round(score, 3)
