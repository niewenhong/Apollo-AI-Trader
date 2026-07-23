"""
ai/param_advisor.py - Apollo Trader v2.6.0
参数建议：基于历史回测最佳参数 + LLM审核，给出优化参数
"""
import json
import numpy as np
from typing import Dict, Optional, List
from core.db_manager import CustomDBManager


class ParamAdvisor:
    """参数建议器"""

    def __init__(self, db: CustomDBManager, llm_client=None):
        self.db = db
        self.llm = llm_client  # 可选LLM增强

    def suggest(self, vt_symbol: str, strategy_class: str,
                current_params: dict = None) -> dict:
        """
        为指定标的+策略生成建议参数
        优先级：回测最佳 > LLM建议 > 默认值
        """
        best = self.db.get_best_params(vt_symbol, strategy_class)
        if best:
            suggestion = best.copy()
            source = "backtest"
        else:
            suggestion = self._default_params(strategy_class)
            source = "default"

        # 如果有LLM，用LLM微调
        if self.llm and hasattr(self.llm, 'refine_params'):
            llm_sug = self.llm.refine_params(vt_symbol, strategy_class, suggestion)
            if llm_sug:
                suggestion.update(llm_sug)
                source = "llm_refined"

        # 写入数据库
        self.db.save_param_suggestion(
            vt_symbol, strategy_class, suggestion,
            source=source, confidence=0.85 if source == "backtest" else 0.6
        )
        return suggestion

    def batch_suggest(self, pool: List[Dict]) -> List[Dict]:
        """批量建议，返回每个标的的建议参数"""
        results = []
        for item in pool:
            sug = self.suggest(item["vt_symbol"], item["strategy_class"])
            results.append({
                "vt_symbol": item["vt_symbol"],
                "strategy_class": item["strategy_class"],
                "params": sug,
            })
        return results

    def _default_params(self, strategy_class: str) -> dict:
        """各策略默认参数"""
        defaults = {
            "MultiIndicatorStrategy": {
                "ma_fast": 5, "ma_slow": 20, "rsi_period": 14,
                "rsi_overbought": 75, "rsi_oversold": 30,
                "atr_period": 14, "atr_multiplier": 2.0,
                "fixed_size": 100,
            },
            "DualThrustStrategy": {
                "k1": 0.618, "k2": 0.382,
                "lookback_days": 5, "fixed_size": 100,
            },
            "SellPutStrategy": {
                "delta_target": 0.25, "days_to_expiry": 45,
                "profit_take_pct": 0.5, "stop_loss_pct": 0.3,
                "max_positions": 5,
            },
            "SellCallStrategy": {
                "delta_target": -0.25, "days_to_expiry": 45,
                "profit_take_pct": 0.5, "stop_loss_pct": 0.3,
                "max_positions": 5,
            },
            "CoveredCallStrategy": {
                "delta_target": 0.2, "days_to_expiry": 45,
                "min_dividend_yield": 0.02,
                "profit_take_pct": 0.5, "stop_loss_pct": 0.3,
                "max_positions": 5,
            },
            "CashSecuredPutStrategy": {
                "delta_target": 0.25, "days_to_expiry": 45,
                "cash_reserve_pct": 0.8,
                "profit_take_pct": 0.5, "stop_loss_pct": 0.3,
                "max_positions": 5,
            },
            "BullCallSpreadStrategy": {
                "delta_long": 0.35, "delta_short": 0.18,
                "min_days_to_expiry": 21, "max_days_to_expiry": 60,
                "min_credit_ratio": 0.2, "rolling_days": 7,
                "max_positions": 5,
            },
            "BearPutSpreadStrategy": {
                "delta_long": -0.38, "delta_short": -0.16,
                "min_days_to_expiry": 21, "max_days_to_expiry": 60,
                "min_credit_ratio": 0.2, "rolling_days": 7,
                "max_positions": 5,
            },
            "IronCondorStrategy": {
                "delta_short_call": 0.19, "delta_short_put": -0.19,
                "wing_width": 0.08,
                "min_days_to_expiry": 30, "max_days_to_expiry": 65,
                "min_credit_ratio": 0.22, "rolling_days": 7,
                "max_positions": 5,
            },
            "StraddleStrategy": {
                "at_the_money_offset": 0.01,
                "min_days_to_expiry": 14, "max_days_to_expiry": 42,
                "min_iv_percentile": 30,
                "profit_target": 2.0, "stop_loss": 0.5,
                "max_positions": 3,
            },
            "IPOStrategy": {
                "min_subscribe_ratio": 80,
                "max_pe_ratio": 150,
                "require_greenshoe": True,
                "first_day_max_hold": 480,
                "profit_take_pct": 0.32,
                "stop_loss_pct": -0.12,
                "max_capital_per_ipo": 80000,
            },
            "CBBCStrategy": {
                "min_leverage": 3.0, "max_leverage": 8.0,
                "min_distance_to_call": 0.04, "max_distance_to_call": 0.15,
                "profit_take_pct": 0.09, "stop_loss_pct": -0.055,
                "max_position_size": 40000,
            },
            "WarrantStrategy": {
                "min_leverage": 4.0, "max_leverage": 12.0,
                "min_days_to_expiry": 14, "max_days_to_expiry": 66,
                "min_delta": 0.2, "max_premium_pct": 0.065,
                "profit_take_pct": 0.075, "stop_loss_pct": -0.045,
                "max_position_size": 35000,
            },
        }
        return defaults.get(strategy_class, {})