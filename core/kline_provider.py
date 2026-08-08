# core/kline_provider.py
"""
KlineProvider - 统一的 K 线数据提供者
封装富途行情接口，支持缓存、重试、多周期、限流

富途官方接口：
- request_history_kline(code, start, end, ktype, autype, fields, max_count, page_req_key)
  返回 (ret, pd.DataFrame, page_req_key) 
- get_rt_kline(code, ktype, num) 获取实时K线 

限流策略：匀速节流（每0.5秒最多1次），限频错误后精确等待窗口恢复
"""
import time
import logging
import re
import threading
import datetime
from typing import Optional, List, Dict, Any
from collections import deque

import pandas as pd
from futu import (
    OpenQuoteContext,
    KLType,
    AuType,
    KL_FIELD,
    RET_OK,
)

logger = logging.getLogger("KlineProvider")


# ==================== 全局速率限制器（单例）====================
class GlobalRateLimiter:
    """
    全局速率限制器（单例模式）
    富途历史K线限制：每30秒最多60次 
    策略：匀速节流，保证两次成功请求间隔 >= 0.5秒
    限频时精确等待窗口恢复，避免固定 sleep(30)。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, max_calls: int = 58, window: float = 30.0):
        if self._initialized:
            return
        self._initialized = True
        self.max_calls = max_calls
        self.window = window
        self._call_times: deque = deque()
        self._lock = threading.Lock()
        self._min_interval = window / max_calls  # 约0.517秒
        self._last_call_time = 0.0

    def acquire(self, block: bool = True) -> bool:
        with self._lock:
            now = time.time()
            wait = self._last_call_time + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            while self._call_times and self._call_times[0] < now - self.window:
                self._call_times.popleft()
            if len(self._call_times) < self.max_calls:
                self._call_times.append(now)
                self._last_call_time = now
                return True
            if not block:
                return False
            wait_until_window = self._call_times[0] + self.window - now + 0.05
        time.sleep(wait_until_window)
        return self.acquire(block=True)

    def remaining(self) -> int:
        now = time.time()
        with self._lock:
            while self._call_times and self._call_times[0] < now - self.window:
                self._call_times.popleft()
            return max(0, self.max_calls - len(self._call_times))


# ==================== KLineProvider ====================
class KlineProvider:
    """
    K 线数据提供者，负责从富途获取历史 K 线和实时 K 线。
    支持缓存、限流（匀速）、自动重试。
    所有 get_* 方法返回 pd.DataFrame 或 None。
    """

    KL_MAP = {
        "K_1M": KLType.K_1M,
        "K_3M": KLType.K_3M,
        "K_5M": KLType.K_5M,
        "K_15M": KLType.K_15M,
        "K_30M": KLType.K_30M,
        "K_60M": KLType.K_60M,
        "K_DAY": KLType.K_DAY,
        "K_WEEK": KLType.K_WEEK,
        "K_MON": KLType.K_MON,
    }

    def __init__(self, quote_ctx: OpenQuoteContext = None, cache_size: int = 500,
                 market: str = None, max_retries: int = 3, auto_type: bool = True):
        self.quote_ctx = quote_ctx
        self._cache: Dict[str, pd.DataFrame] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_max = cache_size
        self._request_count = 0
        self._rate_limiter = GlobalRateLimiter(max_calls=58, window=30.0)
        self.market = market
        self.max_retries = max_retries
        self.auto_type = auto_type

    # ========== ★ 增强版 vt_to_futu：正确处理含点股票代码 ==========
    @staticmethod
    def vt_to_futu(vt_symbol: str) -> str:
        """
        将 vnpy vt_symbol 转换为富途可识别的代码。
        规则：
        - AAPL.SMART   → US.AAPL
        - 00700.SEHK   → HK.00700
        - WSO.B.SMART  → US.WSO.B   （含点代码，保留点）
        - HK.00700     → HK.00700   （已是富途格式，直接返回）
        - US.AAPL      → US.AAPL
        """
        if not vt_symbol:
            return ""
        # 如果已经是富途格式（US./HK. 开头），直接返回
        if vt_symbol.startswith("US.") or vt_symbol.startswith("HK."):
            return vt_symbol

        # 尝试匹配标准格式：symbol.EXCHANGE，其中 symbol 可能含点（如 WSO.B）
        # 使用贪婪匹配最后一段作为交易所后缀
        parts = vt_symbol.rsplit(".", 1)
        if len(parts) != 2:
            return f"US.{vt_symbol}"  # 无法解析，默认美股

        symbol_part, exchange = parts
        exchange_upper = exchange.upper()

        # 交易所映射
        if exchange_upper in ("SEHK", "HKEX", "HK"):
            return f"HK.{symbol_part}"
        elif exchange_upper in ("SMART", "NASDAQ", "NYSE", "US"):
            return f"US.{symbol_part}"
        else:
            # 未知交易所，按原样返回
            logger.warning(f"[KlineProvider] 未知交易所后缀: {exchange} (from {vt_symbol})")
            return f"{exchange}.{symbol_part}"

    # ========== ★ 智能 get_kline：自动识别 vt_symbol 并转换 ==========
    def get_kline(self, code: str, ktype: str = "K_DAY",
                  start: str = None, end: str = None,
                  count: int = 800, refresh_cache: bool = False) -> Optional[pd.DataFrame]:
        if self.quote_ctx is None:
            logger.error("quote_ctx 未设置")
            return None

        # ★ 自动转换：如果 code 看起来像 vt_symbol（包含 .SMART/.SEHK），则转换
        futu_code = code
        if ".SMART" in code.upper() or ".SEHK" in code.upper() or \
           (code.count(".") == 1 and not code.startswith("US.") and not code.startswith("HK.")):
            futu_code = self.vt_to_futu(code)

        cache_key = f"{futu_code}_{ktype}_{count}_{start}_{end}"
        cached = self._cache.get(cache_key)
        cached_time = self._cache_time.get(cache_key, 0)
        now = time.time()
        if not refresh_cache and cached is not None and (now - cached_time) < 30:
            return cached

        kl = self.KL_MAP.get(ktype, KLType.K_DAY)

        for attempt in range(self.max_retries + 1):
            self._rate_limiter.acquire(block=True)
            try:
                if start and end:
                    ret, data, _ = self.quote_ctx.request_history_kline(
                        code=futu_code, start=start, end=end, ktype=kl,
                        autype=AuType.QFQ, fields=[KL_FIELD.ALL], max_count=count
                    )
                else:
                    ret, data, _ = self.quote_ctx.request_history_kline(
                        code=futu_code, ktype=kl,
                        autype=AuType.QFQ, fields=[KL_FIELD.ALL], max_count=count
                    )
            except Exception as e:
                logger.error(f"request_history_kline 异常 {futu_code} {ktype}: {e}")
                return None

            if ret == RET_OK:
                self._cache[cache_key] = data
                self._cache_time[cache_key] = time.time()
                self._request_count += 1
                self._evict_cache()
                return data
            else:
                err_msg = str(data)
                if "频率太高" in err_msg or "频率" in err_msg:
                    logger.warning(f"限频触发 {futu_code} {ktype}，等待窗口恢复 (尝试{attempt+1}/{self.max_retries+1})")
                    continue
                else:
                    logger.warning(f"获取K线失败 {futu_code} {ktype}: {err_msg}")
                    return None

        logger.error(f"获取K线重试耗尽 {futu_code} {ktype}")
        return None

    def _evict_cache(self):
        if len(self._cache) > self._cache_max:
            oldest = min(self._cache_time, key=self._cache_time.get)
            self._cache.pop(oldest, None)
            self._cache_time.pop(oldest, None)

    def get_daily(self, vt_symbol: str, count: int = 250, days: int = None) -> Optional[pd.DataFrame]:
        if days is not None:
            count = days
        code = self.vt_to_futu(vt_symbol)
        if not code:
            return None
        return self.get_kline(code, ktype="K_DAY", count=count)

    def get_weekly(self, vt_symbol: str, count: int = 104,
                   start: str = None, end: str = None) -> Optional[pd.DataFrame]:
        code = self.vt_to_futu(vt_symbol)
        if not code:
            return None
        cache_key = f"{code}_K_WEEK_{count}_{start}_{end}"
        cached = self._cache.get(cache_key)
        cached_time = self._cache_time.get(cache_key, 0)
        now = time.time()
        if cached is not None and (now - cached_time) < 3600:
            return cached
        if not start and not end:
            end_dt = datetime.date.today()
            start_dt = end_dt - datetime.timedelta(days=count * 7 + 730)
            start = start_dt.strftime("%Y-%m-%d")
            end = end_dt.strftime("%Y-%m-%d")
        df = self.get_kline(code, ktype="K_WEEK", start=start, end=end, count=count)
        if df is not None:
            self._cache[cache_key] = df
            self._cache_time[cache_key] = time.time()
        return df

    def get_monthly(self, vt_symbol: str, count: int = 36,
                    start: str = None, end: str = None) -> Optional[pd.DataFrame]:
        code = self.vt_to_futu(vt_symbol)
        if not code:
            return None
        if not start and not end:
            end_dt = datetime.date.today()
            start_dt = end_dt - datetime.timedelta(days=count * 30 + 1095)
            start = start_dt.strftime("%Y-%m-%d")
            end = end_dt.strftime("%Y-%m-%d")
        return self.get_kline(code, ktype="K_MON", start=start, end=end, count=count)

    def get_minute_kline(self, vt_symbol: str, ktype: str = "K_1M",
                         count: int = 780, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
        code = self.vt_to_futu(vt_symbol)
        if not code:
            return None
        return self.get_kline(code, ktype=ktype, start=start, end=end, count=count)

    def get_realtime_kline(self, code: str, ktype: str = "K_1M",
                           num: int = 1) -> Optional[pd.DataFrame]:
        if self.quote_ctx is None:
            return None
        # ★ 同样自动转换 vt_symbol
        futu_code = code
        if ".SMART" in code.upper() or ".SEHK" in code.upper():
            futu_code = self.vt_to_futu(code)
        kl = self.KL_MAP.get(ktype, KLType.K_1M)
        ret, data = self.quote_ctx.get_rt_kline(code=futu_code, ktype=kl, num=num)
        if ret != RET_OK:
            logger.warning(f"获取实时 K 线失败 {futu_code} {ktype}: {data}")
            return None
        return data

    def get_for_diagnosis(self, vt_symbol: str) -> Optional[Dict]:
        code = self.vt_to_futu(vt_symbol)
        if not code:
            return None
        daily_df = self.get_kline(code, ktype="K_DAY", count=365)
        if daily_df is None or daily_df.empty:
            logger.debug(f"诊断 {vt_symbol}: 日线数据为空")
            return None
        weekly_df = self.get_weekly(vt_symbol, count=104)
        result = {
            "daily_df": daily_df,
            "weekly_df": weekly_df,
            "closes": daily_df['close'].values.tolist(),
            "highs": daily_df['high'].values.tolist(),
            "lows": daily_df['low'].values.tolist(),
            "volumes": daily_df['volume'].values.tolist(),
            "latest_close": float(daily_df.iloc[-1]['close']),
            "latest_volume": int(daily_df.iloc[-1]['volume']),
        }
        if weekly_df is not None and not weekly_df.empty:
            result["weekly_closes"] = weekly_df['close'].values.tolist()
            result["weekly_highs"] = weekly_df['high'].values.tolist()
            result["weekly_lows"] = weekly_df['low'].values.tolist()
            result["weekly_volumes"] = weekly_df['volume'].values.tolist()
        else:
            result["weekly_closes"] = None
            result["weekly_highs"] = None
            result["weekly_lows"] = None
            result["weekly_volumes"] = None
        return result

    def clear_cache(self):
        self._cache.clear()
        self._cache_time.clear()
        logger.info("KlineProvider 缓存已清空")

    def get_request_count(self) -> int:
        return self._request_count

    @property
    def cache_size(self) -> int:
        return len(self._cache)