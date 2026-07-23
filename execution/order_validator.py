# -*- coding: utf-8 -*-
"""
订单校验器
- 碎股检查（港股 100 股整数倍、美股 1 股、涡轮/牛熊证 10000 份）
- 最小变动价位对齐
- 价格范围合理性检查
"""
import logging
from typing import Tuple

logger = logging.getLogger("execution.order_validator")

# 各品种最小交易单位
LOT_SIZES = {
    "HK": 100,         # 港股股票
    "US": 1,           # 美股股票
    "HK_WARRANT": 10000, # 港股涡轮
    "HK_CBBC": 10000,    # 港股牛熊证
    "CME": 1,           # 期货
}

# 各品种最小变动价位
MIN_TICKS = {
    "HK": 0.05,       # 港股最小变价
    "US": 0.01,       # 美股
    "HK_WARRANT": 0.001,
    "HK_CBBC": 0.001,
    "CME": 0.25,
}


def validate_order_size(symbol: str, exchange: str, size: int) -> int:
    """
    校验并对齐下单手数到合法的最小交易单位
    :return: 对齐后的手数（可能为 0 = 拒绝）
    """
    # 判断品种类型
    if "WARR" in symbol.upper() or "CBBC" in symbol.upper():
        lot_key = "HK_WARRANT" if "WARR" in symbol.upper() else "HK_CBBC"
    elif exchange in ("HKEX", "HK"):
        lot_key = "HK"
    elif exchange in ("SMART", "NASDAQ", "NYSE"):
        lot_key = "US"
    elif exchange in ("CME", "COMEX", "NYMEX"):
        lot_key = "CME"
    else:
        lot_key = "US"  # 默认

    lot_size = LOT_SIZES.get(lot_key, 1)

    if size < lot_size:
        logger.warning(f"[Validator] 手数 {size} < 最小 {lot_size}，拒绝")
        return 0

    # 对齐
    aligned = (size // lot_size) * lot_size
    if aligned != size:
        logger.info(f"[Validator] 手数对齐: {size} → {aligned} ({lot_key})")
    return aligned


def validate_price(price: float, exchange: str, min_price: float = 0.01) -> float:
    """
    校验并对齐价格到最小变动价位
    """
    if price < min_price:
        logger.warning(f"[Validator] 价格 {price} < 最低 {min_price}，拒绝")
        return 0.0

    if exchange in ("HKEX", "HK"):
        tick = MIN_TICKS["HK"]
    elif exchange in ("SMART", "NASDAQ", "NYSE"):
        tick = MIN_TICKS["US"]
    else:
        tick = MIN_TICKS.get(exchange, 0.01)

    aligned = round(round(price / tick) * tick, 4)
    return aligned


def validate_order(symbol: str, exchange: str,
                  price: float, size: int) -> Tuple[float, int]:
    """
    完整订单校验
    :return: (校验后的价格, 校验后的手数)
    """
    valid_size = validate_order_size(symbol, exchange, size)
    if valid_size == 0:
        return 0.0, 0
    valid_price = validate_price(price, exchange)
    if valid_price == 0.0:
        return 0.0, 0
    return valid_price, valid_size
