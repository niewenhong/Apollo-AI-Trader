# -*- coding: utf-8 -*-
"""
Shelly 下单量算法
根据账户净值、单笔风险百分比、ATR 止损距离，计算最优下单手数。
确保不买碎股（港股 100 股整数倍、美股 1 股、涡轮/牛熊证 10000 份）。
"""
import math

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
    :param risk_pct: 单笔风险占比（如 1.0 表示 1%）
    :param entry_price: 入场价
    :param stop_loss_price: 止损价
    :param lot_size: 每手股数（港股100/美股1/涡轮10000）
    :param min_tick: 最小变动价位
    :param max_position: 最大持仓手数限制
    :return: 整数手数（已对齐 lot_size）
    """
    if entry_price <= 0 or stop_loss_price <= 0:
        return 0
    if entry_price == stop_loss_price:
        return 0

    # 每手风险金额
    risk_amount = account_equity * (risk_pct / 100.0)
    per_share_risk = abs(entry_price - stop_loss_price)

    if per_share_risk < min_tick:
        return 0

    # 理论可买股数
    shares = risk_amount / per_share_risk

    # 对齐到 lot_size
    lots = math.floor(shares / lot_size)

    # 上限
    lots = min(lots, max_position)

    return max(0, lots)

def calculate_position_from_atr(
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
    :param atr_value: 当前 ATR 值
    :param atr_multiplier: 止损 = ATR × multiplier
    """
    stop_distance = atr_value * atr_multiplier
    stop_loss = entry_price - stop_distance  # 多头止损
    return calculate_position_size(
        account_equity=account_equity,
        risk_pct=risk_pct,
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        lot_size=lot_size,
        max_position=max_position
    )

def round_to_lot(size: int, lot_size: int) -> int:
    """将手数对齐到最小交易单位"""
    if lot_size <= 0:
        return size
    return (size // lot_size) * lot_size
