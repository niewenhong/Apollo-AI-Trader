"""
core/kline_provider.py — 统一的历史K线数据提供者（带内存缓存 + 限频保护 + 重试）
================================================================================
v1.5 最终修复：
  - 修复 symbol 格式转换（去掉 .SMART / .SEHK 后缀）
  - 空 DataFrame 也写入缓存（TTL 30 秒），避免重复请求
  - 增加返回行数日志，便于排查
  - 预热时机移到合约就绪后（配合 main.py 修改）
"""

import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List
import pandas as pd

try:
    from futu import RET_OK, KLType, AuType
except ImportError:
    RET_OK = 0

    class KLType:
        K_1M = "K_1M"
        K_5M = "K_5M"
        K_15M = "K_15M"
        K_30M = "K_30M"
        K_60M = "K_60M"
        K_DAY = "K_DAY"
        K_WEEK = "K_WEEK"

    class AuType:
        QFQ = "QFQ"
        HFQ = "HFQ"
        NONE = "NONE"


# ---------- 常量 ----------
DEFAULT_DAILY_DAYS = 120
REQUEST_INTERVAL_SEC = 0.35
STALE_CACHE_SEC = 60          # 正常缓存的 TTL（秒）
EMPTY_CACHE_SEC = 30          # 空数据缓存的 TTL（秒），避免频繁请求
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5


class KlineProvider:
    """统一的历史K线提供者。进程内单例，类级缓存共享。"""

    _cache: Dict[Tuple[str, str, int], Tuple[float, pd.DataFrame]] = {}
    _last_request_ts: float = 0.0

    def __init__(self, quote_ctx, market: str = "US",
                 request_interval: float = REQUEST_INTERVAL_SEC,
                 auto_type: str = "QFQ",
                 max_retries: int = MAX_RETRIES):
        self.ctx = quote_ctx
        self.market = market
        self.request_interval = request_interval
        self.auto_type = auto_type
        self.max_retries = max_retries
        self._stats = {"hits": 0, "misses": 0, "errors": 0}

    # ==================== 公开 API ====================

    def get(self, vt_symbol: str, ktype: str = KLType.K_DAY,
            days: int = DEFAULT_DAILY_DAYS) -> pd.DataFrame:
        cache_key = self._make_key(vt_symbol, ktype, days)

        # 1. 查缓存
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, df = cached
            age = time.time() - ts
            ttl = STALE_CACHE_SEC if not df.empty else EMPTY_CACHE_SEC
            if age < ttl:
                self._stats["hits"] += 1
                return df.copy()
            else:
                del self._cache[cache_key]

        # 2. 缓存未命中，回源
        self._stats["misses"] += 1
        df = self._fetch_from_futu(vt_symbol, ktype, days)

        # ★ 无论是否为空，都写入缓存（空数据 TTL 较短）
        if df is not None:
            self._cache[cache_key] = (time.time(), df)
        else:
            self._stats["errors"] += 1
            self._cache[cache_key] = (time.time(), pd.DataFrame())
        return df.copy()

    def get_daily(self, vt_symbol: str,
                  days: int = DEFAULT_DAILY_DAYS) -> pd.DataFrame:
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=days)

    def get_for_diagnosis(self, vt_symbol: str) -> pd.DataFrame:
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=200)

    def get_for_regime(self, vt_symbol: str) -> pd.DataFrame:
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=120)

    def get_for_selector(self, vt_symbol: str) -> pd.DataFrame:
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=120)

    def get_weekly(self, vt_symbol: str, weeks: int = 100) -> pd.DataFrame:
        return self.get(vt_symbol, ktype=KLType.K_WEEK, days=weeks * 7)

    def get_intraday(self, vt_symbol: str, ktype: str, days: int = 5) -> pd.DataFrame:
        return self.get(vt_symbol, ktype=ktype, days=days)

    # ==================== 符号转换 ====================
    @staticmethod
    def vt_to_futu(vt_symbol: str) -> str:
        """
        将任意格式的 vt_symbol 转换为富途认可的股票代码。
        支持格式：
        - AAPL.SMART          → US.AAPL
        - US.AAPL.SMART       → US.AAPL
        - US.AAPL             → US.AAPL
        - 00700.SEHK          → HK.00700
        - HK.00700.SEHK       → HK.00700
        - HK.00700            → HK.00700
        - NVDA                → US.NVDA（无后缀时自动补 US.）
        """
        # 1. 如果已带市场前缀（US./HK./SH./SZ.），先去掉后缀
        for prefix in ("US.", "HK.", "SH.", "SZ."):
            if vt_symbol.startswith(prefix):
                rest = vt_symbol[len(prefix):]
                if "." in rest:
                    sym, _ = rest.rsplit(".", 1)
                    return f"{prefix}{sym}"
                return vt_symbol

        # 2. 不带市场前缀，但有交易所后缀
        if "." not in vt_symbol:
            return f"US.{vt_symbol}"

        sym, exch = vt_symbol.rsplit(".", 1)
        exch_upper = exch.upper()
        if exch_upper in ("SMART", "NASDAQ", "NYSE", "AMEX"):
            return f"US.{sym}"
        if exch_upper in ("SEHK", "HKEX"):
            return f"HK.{sym}"
        return f"US.{sym}"

    @staticmethod
    def futu_to_vt(code: str) -> str:
        if code.startswith("US."):
            return code.split(".", 1)[1] + ".SMART"
        if code.startswith("HK."):
            return code.split(".", 1)[1] + ".SEHK"
        return code

    # ==================== 内部方法 ====================

    def _make_key(self, vt_symbol: str, ktype: str, days: int) -> tuple:
        futu_code = self.vt_to_futu(vt_symbol)
        return (futu_code, ktype, days)

    def _fetch_from_futu(self, vt_symbol: str,
                         ktype: str, days: int) -> pd.DataFrame:
        if self.ctx is None:
            print(f"[KlineProvider] {vt_symbol} ctx 为 None")
            return pd.DataFrame()

        futu_code = self.vt_to_futu(vt_symbol)

        # 限频
        now = time.time()
        elapsed = now - self.__class__._last_request_ts
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)

        end = datetime.now()
        start = end - timedelta(days=int(days * 1.5))
        max_count = max(days * 2, 300)

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                ret, k, *_ = self.ctx.request_history_kline(
                    futu_code,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    ktype=ktype,
                    autype=getattr(AuType, self.auto_type, AuType.QFQ),
                    max_count=max_count,
                )
                self.__class__._last_request_ts = time.time()

                if ret == RET_OK and k is not None:
                    rows = len(k) if not k.empty else 0
                    print(f"[KlineProvider] {futu_code} {ktype} 返回 {rows} 行")
                    if rows > 0:
                        needed = ["time_key", "open", "high", "low", "close", "volume"]
                        if "turnover" in k.columns:
                            needed.append("turnover")
                        df = k[needed].copy()
                        df.sort_values("time_key", inplace=True)
                        df.reset_index(drop=True, inplace=True)
                        if len(df) > days:
                            df = df.iloc[-days:].reset_index(drop=True)
                        return df
                    else:
                        print(f"[KlineProvider] {futu_code} {ktype} 数据为空，将缓存空数据")
                        return pd.DataFrame()
                else:
                    last_err = f"ret={ret}, data={k}"
                    if ret != RET_OK:
                        print(f"[KlineProvider] {futu_code} 请求失败: {k}")

            except Exception as e:
                last_err = f"异常: {e}"
                print(f"[KlineProvider] {futu_code} 请求异常: {e}")
                self.__class__._last_request_ts = time.time()

            if attempt < self.max_retries:
                wait = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"[KlineProvider] {futu_code} {ktype} 第{attempt}次失败，{wait:.1f}s 后重试 | {last_err}")
                time.sleep(wait)

        print(f"[KlineProvider] {futu_code} {ktype} 全部{self.max_retries}次失败 | {last_err}")
        return pd.DataFrame()

    # ==================== 批量预热 ====================

    def preload(self, vt_symbols: list, ktype: str = KLType.K_DAY,
                days: int = DEFAULT_DAILY_DAYS):
        print(f"[KlineProvider] 预热 {len(vt_symbols)} 只, ktype={ktype}, days={days}")
        for sym in vt_symbols:
            self.get(sym, ktype=ktype, days=days)
        s = self.stats()
        print(f"[KlineProvider] 预热完成: hits={s['hits']} misses={s['misses']} "
              f"errors={s['errors']} hit_rate={s['hit_rate_pct']}%")

    def preload_for_subscription_plan(self, vt_symbols: List[str],
                                       subtypes: List[str],
                                       days_override: Optional[dict] = None):
        subtype_to_ktype = {
            "K_1M": KLType.K_1M, "K_5M": KLType.K_5M, "K_15M": KLType.K_15M,
            "K_30M": KLType.K_30M, "K_60M": KLType.K_60M, "K_DAY": KLType.K_DAY,
        }
        default_days = {
            KLType.K_1M: 2, KLType.K_5M: 10, KLType.K_15M: 30,
            KLType.K_30M: 45, KLType.K_60M: 60, KLType.K_DAY: 120,
        }
        if days_override:
            default_days.update(days_override)

        for st in subtypes:
            ktype = subtype_to_ktype.get(st)
            if ktype is None:
                continue
            days = default_days.get(ktype, 30)
            print(f"[KlineProvider] 预热 {len(vt_symbols)} 只, ktype={st}, days={days}")
            for sym in vt_symbols:
                self.get(sym, ktype=ktype, days=days)
        s = self.stats()
        print(f"[KlineProvider] 按订阅计划预热完成: "
              f"hits={s['hits']} misses={s['misses']} "
              f"errors={s['errors']} hit_rate={s['hit_rate_pct']}%")

    # ==================== 统计 / 调试 ====================

    def stats(self) -> dict:
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total * 100) if total else 0
        return {
            **self._stats,
            "total_requests": total,
            "hit_rate_pct": round(hit_rate, 1),
            "cache_size": len(self._cache),
        }

    def clear_cache(self):
        self._cache.clear()
        self._stats = {"hits": 0, "misses": 0, "errors": 0}