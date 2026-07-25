"""
strategy_matcher.py — 策略匹配器 v2.7.0
功能：根据历史K线检测市场状态，匹配合适策略
版本：v2.7.0
变更：2026-07-26 修复 load_bars 参数传递（提取 code 字段）
"""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class StrategyMatcher:
    def __init__(self, db):
        self.db = db

    def detect_regime(self, symbol: str) -> str:
        """根据15分钟K线检测市场状态"""
        bars = self.db.load_bars(symbol, "15m", limit=100)
        if bars.empty:
            return "neutral"
        closes = bars["close"].values
        if len(closes) < 20:
            return "neutral"
        ma20 = closes[-20:].mean()
        ma5 = closes[-5:].mean()
        if ma5 > ma20:
            return "bullish"
        elif ma5 < ma20 * 0.98:
            return "bearish"
        return "neutral"

    def match(self, selected: List[Dict]) -> List[Dict]:
        """
        匹配策略，返回结果列表
        selected: AIStockSelector 输出的字典列表，每项含 code, vt_symbol, score 等
        """
        results = []
        for item in selected:
            code = item.get("code")  # 例如 "US.NVDA"
            if not code:
                continue
            regime = self.detect_regime(code)
            strategy = self._choose_strategy(regime, item["score"])
            results.append({
                "symbol": code,
                "strategy": strategy,
                "params": {},
                "score": item["score"],
                "regime": regime,
            })
        return results

    def _choose_strategy(self, regime: str, score: float) -> str:
        if regime == "bullish" and score >= 80:
            return "MACD金叉"
        elif regime == "bullish":
            return "均线多头"
        elif regime == "bearish":
            return "防守观望"
        else:
            return "震荡高抛低吸"