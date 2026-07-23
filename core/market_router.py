"""
Apollo-AI-Trader v2.0 - 市场路由与交易时段
功能：
  1. 识别交易所后缀（SEHK/SMART）→ 市场（hk/us）
  2. 港股/美股交易时段判断（含午休）
  3. 美国夏令时（DST）自动检测
  4. 开市前 10min 预热调度
  5. 夏令时自动切换美股交易时段偏移
"""

import pytz
from datetime import datetime, time, timedelta

# ─── 时区 ───────────────────────────────────────
HK_TZ = pytz.timezone("Asia/Hong_Kong")
US_EASTERN = pytz.timezone("US/Eastern")

# ─── 交易所 → 市场映射 ────────────────────────
EXCHANGE_MARKET = {
    "SEHK":   "hk",
    "SMART":  "us",
    "HKFE":   "hk",
    "ISLAND": "us",
    "NYSE":   "us",
    "NASDAQ": "us",
}

# ─── 港股交易时段（不含午休，on_tick 阶段只看是否开盘期内）──
HK_SESSION_1 = (time(9, 30), time(12, 0))   # 早市
HK_SESSION_2 = (time(13, 0), time(16, 0))   # 午市（16:00-16:10 收市竞价不算）
HK_CLOSE_BUFFER = time(16, 10)                  # 收市竞价结束

# ─── 美股交易时段（ET 常规）──
US_REGULAR_OPEN  = time(9, 30)
US_REGULAR_CLOSE = time(16, 0)

# ─── 港股/美股盘前预热分钟数 ──────────────────
PRE_OPEN_MINUTES = 10


# ═════════════════════════════════════════════
#  夏令时检测
# ═════════════════════════════════════════════
def is_dst(dt: datetime | None = None) -> bool:
    """
    美国东部夏令时检测
    规则：3月第二个周日 02:00 → 11月第一个周日 02:00
    """
    dt = dt or datetime.now(US_EASTERN)
    if dt.tzinfo is None:
        dt = US_EASTERN.localize(dt)
    return bool(dt.dst() != timedelta(0))


def us_open_offset_hour() -> int:
    """美股开盘北京时间（夏令时=21:30, 冬令时=22:30）"""
    return 21 if is_dst() else 22


def us_close_hour() -> int:
    """美股收盘北京时间（夏令时=04:00, 冬令时=05:00）"""
    return 4 if is_dst() else 5


# ═════════════════════════════════════════════
#  vt_symbol 解析
# ═════════════════════════════════════════════
def parse_vt(vt_symbol: str) -> tuple[str, str]:
    """
    QQQ.SMART  → ('QQQ', 'SMART')
    02800.SEHK  → ('02800', 'SEHK')
    """
    parts = vt_symbol.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid vt_symbol: {vt_symbol}")
    return parts[0], parts[1]


def detect_market(vt_symbol: str) -> str:
    """从交易所后缀判断市场: SEHK→hk, SMART→us"""
    _, exch = parse_vt(vt_symbol)
    return EXCHANGE_MARKET.get(exch, "unknown")


def exchange_for(vt_symbol: str) -> str:
    """QQQ.SMART → 'SMART'"""
    _, exch = parse_vt(vt_symbol)
    return exch


# ═════════════════════════════════════════════
#  交易时段判断
# ═════════════════════════════════════════════
def is_trading_now(vt_symbol: str, dt: datetime | None = None) -> bool:
    """
    判断当前是否在该标的的交易时段内
    - 港股：周一至周五 09:30-12:00 或 13:00-16:00
    - 美股：周一至周五 09:30-16:00 ET（自动 DST 调整）
    """
    market = detect_market(vt_symbol)
    if market == "unknown":
        return False
    if dt is None:
        dt = datetime.now()
    if dt.weekday() >= 5:   # 周末不开
        return False

    if market == "hk":
        hk_now = dt.astimezone(HK_TZ).time()
        return (HK_SESSION_1[0] <= hk_now <= HK_SESSION_1[1]) or \
               (HK_SESSION_2[0] <= hk_now <= HK_SESSION_2[1])

    if market == "us":
        us_now = dt.astimezone(US_EASTERN).time()
        return US_REGULAR_OPEN <= us_now <= US_REGULAR_CLOSE

    return False


def is_hk_trading(dt: datetime | None = None) -> bool:
    """是否港股交易时段"""
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return False
    hk_now = dt.astimezone(HK_TZ).time()
    return (HK_SESSION_1[0] <= hk_now <= HK_SESSION_1[1]) or \
           (HK_SESSION_2[0] <= hk_now <= HK_SESSION_2[1])


def is_us_trading(dt: datetime | None = None) -> bool:
    """是否美股交易时段（含 DST 自动调整）"""
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return False
    us_now = dt.astimezone(US_EASTERN).time()
    return US_REGULAR_OPEN <= us_now <= US_REGULAR_CLOSE


# ═════════════════════════════════════════════
#  市场检测（北京时间驱动）
# ═════════════════════════════════════════════
def detect_target_market(dt: datetime | None = None) -> str:
    """
    根据当前北京时间判断应该跑哪个市场
    港股：09:30-16:00 北京
    美股：夏令 21:30-04:00 / 冬令 22:30-05:00 北京
    """
    dt = dt or datetime.now()
    h, m = dt.hour, dt.minute

    # 港股时段
    if (9, 30) <= (h, m) <= (16, 0):
        return "HK"

    # 美股时段（跨午夜）
    u_open = us_open_offset_hour()
    u_close = us_close_hour()
    if (u_open, 30) <= (h, m) or (h, m) <= (u_close, 0):
        return "US"

    # 中间地带（港股收盘后到美股开盘前），保持美股不断连
    return "US"


# ═════════════════════════════════════════════
#  开市预热调度
# ═════════════════════════════════════════════
def next_wakeup(dt: datetime | None = None) -> dict | None:
    """
    返回下一个要开市的市场和开市前 PRE_OPEN_MINUTES 的 UTC 时间
    用于 daemon 的预热重连调度
    """
    dt = dt or datetime.now(pytz.UTC)

    # 港股开市前（HK 09:20 = UTC 01:20 冬令 / 00:20 夏令）
    hk_open = datetime.now(HK_TZ).replace(
        hour=9, minute=PRE_OPEN_MINUTES, second=0, microsecond=0)
    if hk_open.weekday() >= 5:
        # 顺到下周一
        days_ahead = (7 - hk_open.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        hk_open += timedelta(days=days_ahead)
    hk_open_utc = hk_open.astimezone(pytz.UTC)

    # 美股开市前（ET 09:20，DST 自动）
    us_eastern_now = datetime.now(US_EASTERN)
    us_open = us_eastern_now.replace(
        hour=9, minute=PRE_OPEN_MINUTES, second=0, microsecond=0)
    if us_open.weekday() >= 5:
        days_ahead = (7 - us_open.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        us_open += timedelta(days=days_ahead)
    us_open_utc = us_open.astimezone(pytz.UTC)

    candidates = []
    if hk_open_utc > dt:
        candidates.append({"market": "hk", "at": hk_open_utc})
    if us_open_utc > dt:
        candidates.append({"market": "us", "at": us_open_utc})

    if not candidates:
        return None
    return min(candidates, key=lambda x: x["at"])


# ═════════════════════════════════════════════
#  收盘判断
# ═════════════════════════════════════════════
def is_hk_closed(dt: datetime | None = None) -> bool:
    """港股是否已收盘（含收市竞价 16:10 后）"""
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return True
    hk_now = dt.astimezone(HK_TZ).time()
    return hk_now >= HK_CLOSE_BUFFER


def is_us_closed(dt: datetime | None = None) -> bool:
    """美股是否已收盘（ET 16:00 后）"""
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return True
    us_now = dt.astimezone(US_EASTERN).time()
    return us_now >= US_REGULAR_CLOSE


# ═════════════════════════════════════════════
#  调试/日志辅助
# ═════════════════════════════════════════════
def market_status_summary(dt: datetime | None = None) -> str:
    """返回当前市场状态的人类可读摘要"""
    dt = dt or datetime.now()
    target = detect_target_market(dt)
    dst = "DST" if is_dst(dt) else "STD"
    hk_trading = "🟢" if is_hk_trading(dt) else "🔴"
    us_trading = "🟢" if is_us_trading(dt) else "🔴"
    return (
        f"[Market] target={target} | DST={dst} | "
        f"HK={hk_trading} US={us_trading} | "
        f"北京时间={dt.strftime('%H:%M:%S')}"
    )
