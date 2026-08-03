"""
core/subscription_plan.py - Apollo Trader v3.2.0
==============================================
按需订阅管理：
  - 根据选股结果自动推断需要订阅的K线周期
  - 异动检测需要 K_1M + QUOTE
  - 基本盘需要 K_DAY
  - 涡轮/牛熊/期权需要正股 QUOTE
  - 不浪费富途300配额
"""
import logging
from typing import Dict, List, Set, Tuple, Optional
from futu import SubType, RET_OK, Session

log = logging.getLogger("SubPlan")

# 每只股票需要的订阅类型映射
ANOMALY_SUBS = ["QUOTE", "K_1M"]     # 异动检测必需
BASIC_SUBS = ["K_DAY"]                  # 基本盘评分
REGIME_SUBS = ["K_DAY"]                 # Regime 计算
WARRANT_SUBS = ["QUOTE"]               # 涡轮只需要正股报价
CBBC_SUBS = ["QUOTE"]                  # 牛熊证同理
OPTION_SUBS = ["QUOTE"]                # 期权同理
TREND_SUBS = ["K_5M", "K_15M"]      # 趋势策略可选


def build_subscription_plan(selected: List[dict]) -> Dict[str, List[str]]:
    """
    根据选股结果构建订阅计划。
    返回 {futu_symbol: [subtype, ...]}
    """
    plan: Dict[str, Set[str]] = {}

    for item in selected:
        vt = item.get("vt_symbol", "")
        code = item.get("code", "")
        anomaly_type = item.get("anomaly_type", "none")
        asset_class = item.get("asset_class", "EQUITY")
        regime = item.get("regime", "range")
        underling = item.get("underlying", "")

        # 确定 futu_symbol
        futu_sym = code if code else vt
        if not futu_sym:
            continue

        # 确保有 . 分隔
        if "." not in futu_sym:
            market = item.get("market", "US")
            prefix = "US." if market == "US" else "HK."
            futu_sym = f"{prefix}{futu_sym}"

        # ---- 异动股：需要1分钟K线+报价 ----
        if anomaly_type != "none":
            for s in ANOMALY_SUBS:
                plan.setdefault(futu_sym, set()).add(s)

        # ---- 基本盘/Equity：需要日线 ----
        if asset_class == "EQUITY":
            for s in BASIC_SUBS:
                plan.setdefault(futu_sym, set()).add(s)

        # ---- Regime 计算需要日线 ----
        if asset_class == "EQUITY":
            for s in REGIME_SUBS:
                plan.setdefault(futu_sym, set()).add(s)

        # ---- 涡轮/牛熊/期权：需要正股报价 ----
        if asset_class in ("HK_WARRANT", "HK_CBBC_BULL", "HK_CBBC_BEAR",
                          "US_OPTION_CALL", "US_OPTION_PUT", "US_OPTION_BOTH"):
            if underling:
                plan.setdefault(underling, set()).add("QUOTE")
            else:
                # 没有正股信息，用自身
                plan.setdefault(futu_sym, set()).add("QUOTE")

        # ---- 趋势策略额外订阅 ----
        if regime == "strong_bull" and asset_class == "EQUITY":
            for s in TREND_SUBS:
                plan.setdefault(futu_sym, set()).add(s)

    # 转换为列表格式
    result = {}
    for sym, subs in plan.items():
        result[sym] = sorted(list(subs))

    log.info(f"[SubPlan] 订阅计划: {len(result)} 只标的")
    for sym, subs in sorted(result.items()):
        log.info(f"  {sym}: {subs}")

    return result


def apply_subscription_plan(sub_manager, config: dict,
                              deployed_strategies: set = None) -> bool:
    """
    应用订阅计划到 SubscriptionManager。
    按需订阅，不浪费配额。
    """
    selected = config.get("selected_stocks", [])
    if not selected:
        log.warning("[SubPlan] 无选股数据，跳过订阅")
        return False

    plan = build_subscription_plan(selected)

    success_count = 0
    for futu_sym, subs in plan.items():
        # 确定用户（用于引用计数）
        user = "selector_plan"
        if sub_manager.subscribe_demand(futu_sym, user, subs):
            success_count += 1
        else:
            log.warning(f"[SubPlan] 订阅失败: {futu_sym} {subs}")

    log.info(f"[SubPlan] ✅ 订阅完成: {success_count}/{len(plan)}")
    return success_count == len(plan)


def get_required_quota(selected: List[dict]) -> int:
    """预估需要的配额数量"""
    plan = build_subscription_plan(selected)
    total = 0
    for subs in plan.values():
        total += len(subs)
    return total

def _strategy_required_subtypes(strategy_names: set) -> list:
    """根据策略名称返回所需的订阅数据类型"""
    required = {"K_1M", "K_DAY", "QUOTE"}
    # 可根据策略名称扩展（例如某些策略需要 TICKER）
    return list(required)
