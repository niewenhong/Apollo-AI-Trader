"""
core/risk_profiles.py — 风险等级模板
"""
from typing import Dict, Any


class RiskProfiles:
    """风险等级配置"""

    PROFILES: Dict[str, Dict[str, Any]] = {
        "conservative": {
            "name": "保守型",
            "max_position_pct": 0.10,
            "max_drawdown_pct": 0.05,
            "max_leverage": 1.0,
            "allowed_products": ["stock"],
            "preferred_strategies": ["GridStrategy", "VWAPStrategy"],
            "stop_loss_pct": 0.03,
            "daily_loss_limit_pct": 0.02,
        },
        "moderate": {
            "name": "稳健型",
            "max_position_pct": 0.20,
            "max_drawdown_pct": 0.10,
            "max_leverage": 2.0,
            "allowed_products": ["stock", "etf"],
            "preferred_strategies": ["TrendStrategy", "GridStrategy"],
            "stop_loss_pct": 0.05,
            "daily_loss_limit_pct": 0.05,
        },
        "aggressive": {
            "name": "进取型",
            "max_position_pct": 0.30,
            "max_drawdown_pct": 0.15,
            "max_leverage": 3.0,
            "allowed_products": ["stock", "warrant"],
            "preferred_strategies": ["TrendStrategy", "OrderFlowStrategy"],
            "stop_loss_pct": 0.07,
            "daily_loss_limit_pct": 0.08,
        },
        "extreme": {
            "name": "激进型",
            "max_position_pct": 0.50,
            "max_drawdown_pct": 0.25,
            "max_leverage": 5.0,
            "allowed_products": ["stock", "warrant", "cbbc"],
            "preferred_strategies": ["TrendStrategy", "OrderFlowStrategy", "DualThrust"],
            "stop_loss_pct": 0.08,
            "daily_loss_limit_pct": 0.12,
        },
    }

    @classmethod
    def get(cls, profile: str) -> dict:
        if profile not in cls.PROFILES:
            log_msg = f"未知风险等级: {profile}，使用默认moderate"
            print(f"[RiskProfiles] {log_msg}")
            return cls.PROFILES["moderate"]
        return cls.PROFILES[profile]

    @classmethod
    def list_profiles(cls) -> dict:
        return {k: v["name"] for k, v in cls.PROFILES.items()}

    @classmethod
    def validate_override(cls, profile: str, overrides: dict) -> dict:
        """验证自定义覆盖参数"""
        base = cls.get(profile).copy()
        for key, value in overrides.items():
            if key in base:
                base[key] = value
        return base