# -*- coding: utf-8 -*-
"""
core/strategy_matcher.py - v3.5.0 增强版
==========================================
变更：
  v3.5.0 - 新增高频策略评分（Scalping/VWAP/OrderFlow）
           - 新增 micro_state 因子支持
           - 新增 get_all_strategies_for_regime() 排名查询
           - IV 百分位 5 桶决策完善
"""
from typing import Optional, Dict, List, Tuple
import logging

log = logging.getLogger("StrategyMatcher")


class StrategyMatcher:
    """
    Regime → 策略 Sharpe 评分匹配器 v3.5.0

    评分维度：
      1) regime × strategy Sharpe 基础分
      2) 期权 IV 百分位修正
      3) regime 置信度加权
      4) 衍生品类型缩放
      5) 高频策略 micro_state 修正
    """

    # ========== Sharpe 表（regime × strategy）==========
    SHARPE_TABLE = {
        # ── strong_bull ──
        "strong_bull": {
            "TrendStrategy": 1.8, "MomentumStrategy": 1.6,
            "DualThrustStrategy": 1.4, "VWAPStrategy": 1.3,
            "GridStrategy": 0.8, "SellCallStrategy": 1.5,
            "CoveredCallStrategy": 1.2, "BullCallSpreadStrategy": 1.7,
            "CashSecuredPutStrategy": 0.9, "IPOStrategy": 1.4,
            "ScalpingStrategy": 1.3, "OrderFlowStrategy": 1.4,
        },
        # ── bull ──
        "bull": {
            "TrendStrategy": 1.5, "MomentumStrategy": 1.4,
            "DualThrustStrategy": 1.3, "VWAPStrategy": 1.2,
            "GridStrategy": 1.0, "BullCallSpreadStrategy": 1.5,
            "CoveredCallStrategy": 1.1, "SellPutStrategy": 1.0,
            "IPOStrategy": 1.2, "ScalpingStrategy": 1.2,
        },
        # ── range ──
        "range": {
            "GridStrategy": 1.8, "DualThrustStrategy": 1.5,
            "SellCallStrategy": 1.6, "SellPutStrategy": 1.5,
            "IronCondorStrategy": 1.7, "StraddleStrategy": 0.9,
            "VWAPStrategy": 1.2, "CashSecuredPutStrategy": 1.3,
            "CoveredCallStrategy": 1.0, "ScalpingStrategy": 1.0,
        },
        # ── volatile ──
        "volatile": {
            "DualThrustStrategy": 1.7, "MomentumStrategy": 1.5,
            "StraddleStrategy": 1.6, "GridStrategy": 0.6,
            "TrendStrategy": 0.8, "IronCondorStrategy": 1.3,
            "BearPutSpreadStrategy": 1.4, "BullCallSpreadStrategy": 1.2,
            "ScalpingStrategy": 1.6, "OrderFlowStrategy": 1.5,
            "VWAPStrategy": 1.0,
        },
        # ── weak_bear / bear ──
        "weak_bear": {
            "BearPutSpreadStrategy": 1.6, "MomentumStrategy": 1.3,
            "DualThrustStrategy": 1.2, "SellCallStrategy": 1.4,
            "GridStrategy": 0.7, "CashSecuredPutStrategy": 1.0,
            "ScalpingStrategy": 1.1,
        },
        "bear": {
            "BearPutSpreadStrategy": 1.7, "MomentumStrategy": 1.5,
            "DualThrustStrategy": 1.3, "SellCallStrategy": 1.5,
            "CashSecuredPutStrategy": 1.1, "ScalpingStrategy": 1.0,
        },
        # ── 衍生品专用 regime ──
        "range_high_iv": {
            "SellCallStrategy": 2.0, "SellPutStrategy": 1.9,
            "IronCondorStrategy": 2.1, "StraddleStrategy": 0.5,
        },
        "volatile_low_iv": {
            "StraddleStrategy": 1.8, "BullCallSpreadStrategy": 1.5,
            "BearPutSpreadStrategy": 1.5,
        },
    }

    # ========== IV 百分位修正 ==========
    IV_BUCKETS = {
        "very_low":  (0.0,  0.2),
        "low":       (0.2,  0.4),
        "medium":    (0.4,  0.6),
        "high":      (0.6,  0.8),
        "very_high": (0.8,  1.01),
    }

    IV_DIRECTION = {
        "very_low":  {"buy": 0.3, "sell": -0.2},
        "low":       {"buy": 0.15, "sell": -0.1},
        "medium":    {"buy": 0.0, "sell": 0.0},
        "high":      {"buy": -0.1, "sell": 0.15},
        "very_high": {"buy": -0.2, "sell": 0.3},
    }

    BUY_STRATEGIES = {
        "BullCallSpreadStrategy", "BearPutSpreadStrategy",
        "StraddleStrategy", "MomentumStrategy",
    }
    SELL_STRATEGIES = {
        "SellCallStrategy", "SellPutStrategy",
        "CoveredCallStrategy", "CashSecuredPutStrategy",
        "IronCondorStrategy",
    }

    # ========== 高频策略 micro_state 修正 ==========
    MICRO_STATE_MODIFIERS = {
        "toxic_orderflow": {
            "ScalpingStrategy": +0.3, "VWAPStrategy": -0.1, "OrderFlowStrategy": +0.2,
        },
        "liquidity_surge": {
            "OrderFlowStrategy": +0.3, "ScalpingStrategy": +0.1, "VWAPStrategy": -0.1,
        },
        "tight_spread": {
            "VWAPStrategy": +0.2, "ScalpingStrategy": +0.1,
        },
        "normal": {},
    }

    # ========== 衍生品缩放 ==========
    DERIVATIVE_PENALTY = {
        "WARRANT": 0.85,
        "CBBC": 0.85,
        "OPTION": 0.95,
        "IPO": 0.9,
        "EQUITY": 1.0,
    }

    # ========== 置信度权重 ==========
    CONFIDENCE_WEIGHTS = {
        "high":   1.0,
        "medium": 0.8,
        "low":    0.5,
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self._override: Dict[str, float] = {}

    # ==================== 公开 API ====================

    def match(self, regime: str, asset_class: str = "EQUITY",
              iv_percentile: float = 0.5,
              confidence: float = 0.5,
              micro_state: str = "normal",
              top_n: int = 5) -> List[Tuple[str, float]]:
        """
        返回 [(strategy_name, adjusted_sharpe), ...] 降序
        """
        base_table = self.SHARPE_TABLE.get(regime, {})
        if not base_table:
            log.warning(f"[Matcher] 未知 regime: {regime}，使用 range 兜底")
            base_table = self.SHARPE_TABLE["range"]

        # 1) IV 修正
        iv_bucket = self._bucket_iv(iv_percentile)
        iv_dir = self.IV_DIRECTION[iv_bucket]

        # 2) 置信度权重
        conf_weight = self._confidence_weight(confidence)

        # 3) 衍生品缩放
        deriv_scale = self.DERIVATIVE_PENALTY.get(asset_class, 1.0)

        # 4) 高频 micro_state 修正
        micro_mod = self.MICRO_STATE_MODIFIERS.get(micro_state, {})

        scored = []
        for sname, base_sharpe in base_table.items():
            if sname in self._override:
                sharpe = self._override[sname]
            else:
                sharpe = base_sharpe

            # IV 方向修正
            if sname in self.BUY_STRATEGIES:
                sharpe += iv_dir["buy"]
            elif sname in self.SELL_STRATEGIES:
                sharpe += iv_dir["sell"]

            # micro_state 修正
            if sname in micro_mod:
                sharpe += micro_mod[sname]

            # 置信度 + 衍生品缩放
            sharpe *= conf_weight
            sharpe *= deriv_scale

            sharpe = max(sharpe, 0.1)
            scored.append((sname, round(sharpe, 3)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    def get_best(self, regime: str, asset_class: str = "EQUITY",
                 iv_percentile: float = 0.5,
                 confidence: float = 0.5,
                 micro_state: str = "normal") -> str:
        results = self.match(regime, asset_class, iv_percentile, confidence, micro_state, top_n=1)
        return results[0][0] if results else "GridStrategy"

    def get_all_strategies_for_regime(self, regime: str,
                                      asset_class: str = "EQUITY",
                                      iv_percentile: float = 0.5,
                                      confidence: float = 0.5,
                                      micro_state: str = "normal") -> List[dict]:
        results = self.match(regime, asset_class, iv_percentile, confidence, micro_state, top_n=50)
        output = []
        for rank, (sname, score) in enumerate(results, 1):
            output.append({
                "rank": rank,
                "strategy": sname,
                "adjusted_sharpe": score,
                "regime": regime,
                "asset_class": asset_class,
                "iv_percentile": iv_percentile,
                "confidence": confidence,
                "micro_state": micro_state,
            })
        return output

    def recommend_with_weights(self, regime: str,
                               weights: Optional[Dict[str, float]] = None,
                               asset_class: str = "EQUITY",
                               iv_percentile: float = 0.5,
                               confidence: float = 0.5,
                               micro_state: str = "normal") -> List[Tuple[str, float]]:
        base = self.match(regime, asset_class, iv_percentile, confidence, micro_state, top_n=50)
        if not weights:
            return base
        scored = []
        for sname, sharpe in base:
            w = weights.get(sname, 1.0)
            scored.append((sname, round(sharpe * w, 3)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def set_override(self, strategy_name: str, sharpe: float):
        self._override[strategy_name] = sharpe

    def clear_overrides(self):
        self._override.clear()

    # ==================== 内部 ====================

    def _bucket_iv(self, pct: float) -> str:
        pct = max(0.0, min(1.0, pct))
        for name, (lo, hi) in self.IV_BUCKETS.items():
            if lo <= pct < hi:
                return name
        return "medium"

    def _confidence_weight(self, conf: float) -> float:
        if conf >= 0.7:
            return self.CONFIDENCE_WEIGHTS["high"]
        elif conf >= 0.4:
            return self.CONFIDENCE_WEIGHTS["medium"]
        else:
            return self.CONFIDENCE_WEIGHTS["low"]
