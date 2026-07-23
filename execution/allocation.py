# -*- coding: utf-8 -*-
"""
Shelly 下单量算法（详细版）
根据账户净值、风险百分比、ATR 止损距离，计算最优下单手数。
核心思想：每笔交易最多亏损账户净值的 X%，由此反推可买手数。
"""
import math
import logging

logger = logging.getLogger("execution.allocation")


def calculate_position_size(
    account_equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss_price: float,
    lot_size: int = 1,
    min_tick: float = 0.01,
    max_position: int = 100
) -> int:
    """
    Shelly 算法：计算下单手数
    :param account_equity: 账户净值
    :param risk_pct: 单笔风险占比（如 1.0 = 1%）
    :param entry_price: 入场价
    :param stop_loss_price: 止损价
    :param lot_size: 每手股数
    :param min_tick: 最小变动价位
    :param max_position: 最大持仓手数
    :return: 整数手数（已对齐 lot_size），0 = 不可交易
    """
    if account_equity <= 0:
        logger.warning("[Shelly] 账户净值 <= 0")
        return 0
    if entry_price <= 0 or stop_loss_price <= 0:
        logger.warning(f"[Shelly] 价格异常: entry={entry_price} sl={stop_loss_price}")
        return 0

    risk_amount = account_equity * (risk_pct / 100.0)
    per_share_risk = abs(entry_price - stop_loss_price)

    if per_share_risk < min_tick:
        logger.warning(f"[Shelly] 止损距离 {per_share_risk:.4f} < min_tick {min_tick}")
        return 0

    shares = risk_amount / per_share_risk
    lots = math.floor(shares / lot_size)
    lots = min(lots, max_position)
    lots = max(0, lots)

    logger.debug(
        f"[Shelly] equity={account_equity:.2f} risk={risk_pct}% "
        f"risk_amt={risk_amount:.2f} per_share_risk={per_share_risk:.4f} "
        f"→ {lots} 手"
    )
    return lots


def calculate_from_atr(
    account_equity: float,
    risk_pct: float,
    entry_price: float,
    atr_value: float,
    atr_multiplier: float = 1.5,
    lot_size: int = 1,
    max_position: int = 100
) -> int:
    """
    基于 ATR 的 Shelly 仓位计算
    :param atr_value: 当前 ATR
    :param atr_multiplier: 止损 = ATR × multiplier
    """
    stop_distance = atr_value * atr_multiplier
    if entry_price > stop_distance:
        stop_loss = entry_price - stop_distance
    else:
        stop_loss = entry_price * 0.95  # 兜底：至少留 5% 空间
    return calculate_position_size(
        account_equity=account_equity,
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        lot_size=lot_size,
        max_position=max_position
    )


def allocate_across_accounts(
    total_size: int,
    accounts: dict,  # {"acc1": 50000, "acc2": 30000}
    lot_size: int = 1
) -> dict:
    """
    将总手数按账户净值比例分配到多个账户
    :return: {"acc1": 6, "acc2": 4}
    """
    total_equity = sum(accounts.values())
    if total_equity <= 0:
        return {k: 0 for k in accounts}
    allocation = {}
    for acc, eq in accounts.items():
        raw = total_size * (eq / total_equity)
        lots = (int(raw) // lot_size) * lot_size
        allocation[acc] = max(0, lots)
    return allocation


def round_to_lot(size: int, lot_size: int) -> int:
    """将手数对齐到最小交易单位"""
    if lot_size <= 0:
        return size
    return (size // lot_size) * lot_size
