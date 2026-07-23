# -*- coding: utf-8 -*-
"""交易时段判断（港股/美股/期货夜盘/夏令时自动检测）"""
from datetime import datetime, time, timedelta
import pytz

# ===== 港股 =====
HK_REGULAR_OPEN = time(9, 30)
HK_REGULAR_CLOSE = time(16, 0)
HK_LUNCH_START = time(12, 0)
HK_LUNCH_END = time(13, 0)

# ===== 美股 =====
US_EASTERN = pytz.timezone("US/Eastern")

def _is_dst(dt: datetime) -> bool:
    """判断当前是否为美国夏令时"""
    return bool(US_EASTERN.localize(dt).dst())

def _us_regular_open_close(dt: datetime):
    """返回美股当日开盘/收盘时间（本地时间，已考虑夏令时）"""
    is_dst = _is_dst(dt)
    # 美东时间 09:30 - 16:00
    open_t = time(9, 30)
    close_t = time(16, 0)
    # 转为 UTC 再转本地
    open_dt = US_EASTERN.localize(datetime(dt.year, dt.month, dt.day, open_t.hour, open_t.minute))
    close_dt = US_EASTERN.localize(datetime(dt.year, dt.month, dt.day, close_t.hour, close_t.minute))
    return open_dt, close_dt

def is_trading_hour(symbol: str, exchange: str, dt: datetime = None) -> bool:
    """
    判断当前是否在该标的的交易时段内
    :param symbol: 标的代码
    :param exchange: 交易所代码
    :param dt: 当前时间（默认 now）
    """
    if dt is None:
        dt = datetime.now()

    # 期货夜盘（CME 主力品种）
    if exchange in ("CME", "COMEX", "NYMEX"):
        # 简化：日盘 6:00-17:00 美东 + 夜盘 18:00-次日5:00
        return True  # CME 几乎全天交易，具体品种另行细化

    # 港股
    if exchange in ("HKEX", "HK"):
        t = dt.time()
        # 排除午休
        if HK_LUNCH_START <= t < HK_LUNCH_END:
            return False
        return HK_REGULAR_OPEN <= t <= HK_REGULAR_CLOSE

    # 美股
    if exchange in ("SMART", "NASDAQ", "NYSE", "CME"):
        open_dt, close_dt = _us_regular_open_close(dt)
        # 转为同一时区比较
        if dt.tzinfo is None:
            dt_utc = pytz.utc.localize(dt)
        else:
            dt_utc = dt
        return open_dt <= dt_utc <= close_dt

    return True  # 未知交易所默认放行

def is_us_regular_hours(dt: datetime = None) -> bool:
    """是否在美股常规交易时段"""
    if dt is None:
        dt = datetime.utcnow()
    open_dt, close_dt = _us_regular_open_close(dt)
    return open_dt <= pytz.utc.localize(dt) <= close_dt

def is_hk_regular_hours(dt: datetime = None) -> bool:
    """是否在港股常规交易时段"""
    if dt is None:
        dt = datetime.now()
    t = dt.time()
    if HK_LUNCH_START <= t < HK_LUNCH_END:
        return False
    return HK_REGULAR_OPEN <= t <= HK_REGULAR_CLOSE
