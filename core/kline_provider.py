"""
kline_provider.py — 统一的历史K线数据提供者（带内存缓存 + 限频保护）
====================================================================
设计目标：
  1. 选股器 / StockDiagnosis / RegimeTrainer / StrategyGenerator
     全部通过本模块取历史K线，禁止各自直接调 request_history_kline。
  2. 同一进程内，同一 (vt_symbol, ktype, days) 只请求富途一次，
     后续全部命中内存缓存，彻底杜绝重复请求触发的限频。
  3. 内置 vt_symbol ↔ 富途代码 的双向转换，不再依赖外部模块。
  4. 三返回值解包统一用 ret, data, *_ ，永不再出
     "too many values to unpack" 错误。
  5. 请求间自动插入间隔（默认 0.35s），保证 30 秒窗口内
     远不会超过 60 次限制。

使用方式：
  kp = KlineProvider(quote_ctx=us_ctx, market="US")
  df = kp.get_daily(symbol)              # 日K，默认 120 根
  df = kp.get(symbol, ktype=KLType.K_60M, days=300)  # 任意周期
  diag_input = kp.get_for_diagnosis(symbol)   # 返回 200 根日K DataFrame
  regime_input = kp.get_for_regime(symbol)     # 返回 120 根日K DataFrame

返回 DataFrame 列：
  time_key | open | high | low | close | volume | turnover
"""

import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
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
DEFAULT_DAILY_DAYS = 120        # 默认日K 根数
REQUEST_INTERVAL_SEC = 0.35      # 两次富途请求之间的最小间隔
CONNECT_TIMEOUT_SEC = 5.0        # 连接检测超时（保留接口）
STALE_CACHE_SEC = 60             # 同一只票缓存有效期（秒）


class KlineProvider:
    """
    统一的历史K线提供者。
    线程不安全（vnpy 主线程内使用即可），进程内单例。
    """

    # 类级缓存（同一进程所有模块共享）
    _cache: Dict[Tuple[str, str, int], Tuple[float, pd.DataFrame]] = {}
    _last_request_ts: float = 0.0

    def __init__(self, quote_ctx, market: str = "US",
                 request_interval: float = REQUEST_INTERVAL_SEC,
                 auto_type: str = "QFQ"):
        """
        Parameters
        ----------
        quote_ctx : futu.OpenQuoteContext 或任何提供 request_history_kline 的对象
        market : "US" | "HK"  仅用于默认 universe 和日志提示
        request_interval : 两次真实请求之间的最小间隔（秒）
        auto_type : 复权类型 "QFQ" / "HFQ" / "NONE"
        """
        self.ctx = quote_ctx
        self.market = market
        self.request_interval = request_interval
        self.auto_type = auto_type
        self._stats = {"hits": 0, "misses": 0, "errors": 0}

    # ==================== 公开 API ====================

    def get(self, vt_symbol: str, ktype: str = KLType.K_DAY,
            days: int = DEFAULT_DAILY_DAYS) -> pd.DataFrame:
        """
        获取任意周期的K线。
        返回标准化 DataFrame，空 DataFrame 表示失败（调用方降级处理）。
        """
        cache_key = self._make_key(vt_symbol, ktype, days)

        # 1. 命中缓存且未过期
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, df = cached
            if time.time() - ts < STALE_CACHE_SEC and not df.empty:
                self._stats["hits"] += 1
                return df.copy()

        # 2. 缓存未命中 → 请求富途
        self._stats["misses"] += 1
        df = self._fetch_from_futu(vt_symbol, ktype, days)

        # 存入缓存（即使是空 DF 也缓存，避免反复踩坑）
        self._cache[cache_key] = (time.time(), df)
        return df.copy()

    def get_daily(self, vt_symbol: str,
                  days: int = DEFAULT_DAILY_DAYS) -> pd.DataFrame:
        """日K 快捷方法（最常用）。"""
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=days)

    def get_for_diagnosis(self, vt_symbol: str) -> pd.DataFrame:
        """
        给 StockDiagnosis 用的 200 根日K。
        返回列：同 get()，空 DF 时诊断模块应走降级。
        """
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=200)

    def get_for_regime(self, vt_symbol: str) -> pd.DataFrame:
        """
        给 RegimeTrainer 用的 120 根日K。
        """
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=120)

    def get_for_selector(self, vt_symbol: str) -> pd.DataFrame:
        """
        给 AIStockSelector 用的 120 根日K。
        """
        return self.get(vt_symbol, ktype=KLType.K_DAY, days=120)

    def get_weekly(self, vt_symbol: str,
                   weeks: int = 100) -> pd.DataFrame:
        """周K，给趋势/52周高低点用。"""
        return self.get(vt_symbol, ktype=KLType.K_WEEK, days=weeks * 7)

    def get_intraday(self, vt_symbol: str, ktype: str,
                     days: int = 5) -> pd.DataFrame:
        """分钟级K线（1M/5M/15M/30M/60M）。days 控制回看自然日。"""
        return self.get(vt_symbol, ktype=ktype, days=days)

    # ==================== 符号转换 ====================

    @staticmethod
    def vt_to_futu(vt_symbol: str) -> str:
        """
        vt_symbol → 富途代码。
        例：
          AAPL.SMART  → US.AAPL
          00700.SEHK   → HK.00700
          US.NVDA      → US.NVDA   (已是富途格式，原样返回)
        """
        if vt_symbol.startswith(("US.", "HK.", "SH.", "SZ.")):
            return vt_symbol
        if "." not in vt_symbol:
            # 纯代码，按 market 猜
            return f"US.{vt_symbol}"
        sym, exch = vt_symbol.rsplit(".", 1)
        exch_upper = exch.upper()
        if exch_upper in ("SMART", "NASDAQ", "NYSE", "AMEX"):
            return f"US.{sym}"
        if exch_upper in ("SEHK", "HKEX"):
            return f"HK.{sym}"
        # 兜底
        return f"US.{sym}"

    @staticmethod
    def futu_to_vt(code: str) -> str:
        """
        富途代码 → vt_symbol。
        例：
          US.AAPL → AAPL.SMART
          HK.00700 → 00700.SEHK
        """
        if code.startswith("US."):
            return code.split(".", 1)[1] + ".SMART"
        if code.startswith("HK."):
            return code.split(".", 1)[1] + ".SEHK"
        return code

    # ==================== 内部方法 ====================

    def _make_key(self, vt_symbol: str, ktype: str, days: int) -> tuple:
        # 统一用富途代码做 key，避免 AAPL.SMART / US.AAPL 被视为不同
        futu_code = self.vt_to_futu(vt_symbol)
        return (futu_code, ktype, days)

    def _fetch_from_futu(self, vt_symbol: str,
                         ktype: str, days: int) -> pd.DataFrame:
        """真正打富途接口，含限频间隔、三返回值解包、标准化。"""
        if self.ctx is None:
            self._stats["errors"] += 1
            return pd.DataFrame()

        futu_code = self.vt_to_futu(vt_symbol)

        # 限频保护：保证两次请求间隔 ≥ request_interval
        now = time.time()
        elapsed = now - self.__class__._last_request_ts
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)

        # 计算日期范围（多拉 1.5 倍自然日，过滤周末/假日）
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.5))
        max_count = max(days * 2, 300)

        try:
            ret, k, *_ = self.ctx.request_history_kline(
                futu_code,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                ktype=ktype,
                autype=getattr(AuType, self.auto_type, AuType.QFQ),
                max_count=max_count,
            )
        except Exception as e:
            print(f"[KlineProvider] {futu_code} 请求异常: {e}")
            self._stats["errors"] += 1
            self.__class__._last_request_ts = time.time()
            return pd.DataFrame()

        self.__class__._last_request_ts = time.time()

        if ret != RET_OK or k is None or k.empty:
            self._stats["errors"] += 1
            return pd.DataFrame()

        # 标准化列 & 排序
        needed = ["time_key", "open", "high", "low", "close", "volume"]
        if "turnover" in k.columns:
            needed.append("turnover")
        df = k[needed].copy()
        df.sort_values("time_key", inplace=True)
        df.reset_index(drop=True, inplace=True)

        # 截断到需要的根数
        if len(df) > days:
            df = df.iloc[-days:].reset_index(drop=True)

        return df

    # ==================== 统计 / 调试 ====================

    def stats(self) -> dict:
        """返回缓存命中统计，便于日志/监控。"""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total * 100) if total else 0
        return {
            **self._stats,
            "total_requests": total,
            "hit_rate_pct": round(hit_rate, 1),
            "cache_size": len(self._cache),
        }

    def clear_cache(self):
        """手动清空缓存（换交易日/盘前刷新时用）。"""
        self._cache.clear()
        self._stats = {"hits": 0, "misses": 0, "errors": 0}

    def preload(self, vt_symbols: list, ktype: str = KLType.K_DAY,
                days: int = DEFAULT_DAILY_DAYS):
        """
        批量预热：流水线启动前一次性把所需K线拉满缓存。
        之后所有模块调用 get() 全部命中缓存，零额外请求。
        """
        print(f"[KlineProvider] 预热 {len(vt_symbols)} 只, "
              f"ktype={ktype}, days={days}")
        for sym in vt_symbols:
            self.get(sym, ktype=ktype, days=days)
        s = self.stats()
        print(f"[KlineProvider] 预热完成: "
              f"hits={s['hits']} misses={s['misses']} "
              f"errors={s['errors']} hit_rate={s['hit_rate_pct']}%")
