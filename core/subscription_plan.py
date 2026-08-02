"""
subscription_plan.py — Apollo AI Trader v3.0.0
核心原则：订阅类型 = 当前实际部署策略真正消费的类型
"""
from typing import Dict, List, Set


# 策略类 → 所需订阅类型的映射
STRATEGY_SUBTYPE_MAP = {
    "GridStrategy":           ["QUOTE", "K_1M"],
    "TrendStrategy":          ["QUOTE", "K_1M"],
    "VWAPStrategy":           ["QUOTE", "K_1M"],
    "DualThrustStrategy":     ["QUOTE", "K_1M"],
    "MultiIndicatorStrategy":  ["QUOTE", "K_1M", "K_DAY", "K_60M"],
    "TickOrderFlowStrategy":  ["QUOTE", "K_1M", "TICKER"],
    "OrderFlowStrategy":      ["QUOTE", "K_1M"],
}


def _strategy_required_subtypes(strategy_classes: Set[str]) -> List[str]:
    """根据已部署策略类反推所需订阅类型（去重、排序）"""
    required = set()
    for cls in strategy_classes:
        for st in STRATEGY_SUBTYPE_MAP.get(cls, ["QUOTE", "K_1M"]):
            required.add(st)
    return sorted(required)


def build_subscription_plan(config: dict,
                            deployed_strategies: Set[str] = None) -> Dict[str, Dict[str, List[str]]]:
    """
    构建订阅计划。
    key: futu_symbol (如 US.NVDA)
    value: {user: [subtypes]}
    """
    if deployed_strategies is None:
        deployed_strategies = {"GridStrategy"}

    subtypes = _strategy_required_subtypes(deployed_strategies)

    universe = config.get("universe", {"US": [], "HK": []})
    plan = {}
    for market, symbols in universe.items():
        for sym in symbols:
            futu_sym = f"{market}.{sym}"
            plan[futu_sym] = {"DynamicPlan": subtypes}
    return plan


def apply_subscription_plan(sub_manager, config: dict,
                             deployed_strategies: Set[str] = None) -> bool:
    """构建并执行订阅计划，返回是否全部成功"""
    plan = build_subscription_plan(config, deployed_strategies)
    success = True
    for symbol, users_plan in plan.items():
        for user, subtypes in users_plan.items():
            if not sub_manager.subscribe_demand(symbol, user, subtypes):
                success = False
    return success
