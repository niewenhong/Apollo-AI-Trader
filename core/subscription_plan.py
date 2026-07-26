"""
core/subscription_plan.py — 订阅计划 v2.8.0
根据 system_config.json 中的 subscription.periods 动态生成订阅计划
"""
from typing import Dict, List


def build_subscription_plan(config: dict) -> Dict[str, Dict[str, List[str]]]:
    """
    根据配置构建订阅计划
    返回: {symbol: {user: [subtypes]}}
    """
    periods = config.get("subscription", {}).get("periods", ["K_1M", "K_5M"])
    universe = config.get("universe", {"US": [], "HK": []})

    # 基础订阅类型：报价 + 逐笔 + 配置的K线
    base_types = ["QUOTE", "TICKER"] + periods

    plan = {}
    for market, symbols in universe.items():
        for symbol in symbols:
            plan[symbol] = {
                "MultiIndicator": base_types,
                "AISelector": ["K_DAY", "K_60M", "QUOTE"],
            }
    return plan


def apply_subscription_plan(sub_manager, config: dict) -> bool:
    """执行订阅计划"""
    plan = build_subscription_plan(config)
    success = True
    for symbol, users_plan in plan.items():
        for user, subtypes in users_plan.items():
            if not sub_manager.subscribe_demand(symbol, user, subtypes):
                success = False
    return success
