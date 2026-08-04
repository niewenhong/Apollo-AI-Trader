# -*- coding: utf-8 -*-
"""
ai/stock_selector.py - 选股模块 v3.6.0
========================================
- 基本盘 + 异动扫描 + IPO
- 衍生品：通过富途 get_warrant() 正确查询窝轮+牛熊证，按 warrant_type 分流
- 返回标准 vt_symbol 格式：XXXX.SEHK
"""
import logging
import numpy as np
from typing import List, Dict, Optional

from core.kline_provider import KlineProvider

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

logger = logging.getLogger("StockSelector")


class StockSelector:
    """选股器 v3.6.0"""

    US_CORE_POOL = [
        "AAPL.SMART", "MSFT.SMART", "GOOGL.SMART", "AMZN.SMART",
        "NVDA.SMART", "META.SMART", "TSLA.SMART", "AMD.SMART",
        "NFLX.SMART", "BABA.SMART", "SPY.SMART", "QQQ.SMART",
        "DIA.SMART", "IWM.SMART", "XLE.SMART", "XLF.SMART",
        "XLK.SMART", "XLV.SMART", "XLI.SMART", "XLB.SMART",
    ]
    HK_CORE_POOL = [
        "00700.SEHK", "09988.SEHK", "03690.SEHK", "01810.SEHK",
        "09999.SEHK", "01211.SEHK", "02382.SEHK", "09618.SEHK",
        "02015.SEHK", "09888.SEHK",
    ]

    VOLUME_SURGE_THRESHOLD = 1.5
    PRICE_CHANGE_THRESHOLD = 0.035
    AMPLITUDE_THRESHOLD = 0.045

    DERIVATIVE_ENABLED = True
    MAX_WARRANTS_PER_STOCK = 3
    MAX_CBBS_PER_STOCK = 3
    MAX_UNDERLYING_SCANNED = 5

    # 富途 get_warrant 返回的 warrant_type 枚举值
    CALL = 1   # 认购
    PUT = 2    # 认沽
    BULL = 3   # 牛证
    BEAR = 4   # 熊证

    def __init__(self, quote_ctx=None, db=None,
                 kline_provider: Optional[KlineProvider] = None,
                 config: Optional[dict] = None):
        self.ctx = quote_ctx
        self.db = db
        self.kp = kline_provider
        self.config = config or {}
        logger.info("[Selector] v3.6.0 初始化完成（get_warrant 正确查询）")

    def run(self, markets: List[str] = None) -> List[Dict]:
        if markets is None:
            markets = ["US", "HK"]
        all_candidates = []
        for mkt in markets:
            logger.info(f"[Selector] === 开始选股: {mkt} ===")
            candidates = []

            pool = self.US_CORE_POOL if mkt == "US" else self.HK_CORE_POOL
            for sym in pool:
                candidates.append({
                    "vt_symbol": sym, "market": mkt,
                    "asset_type": "EQUITY", "anomaly_type": "none",
                    "source": "core_pool", "score": 85, "extra": {},
                })

            candidates.extend(self._scan_anomalies(mkt))

            if mkt == "HK":
                candidates.extend(self._scan_ipo(mkt))
                if self.DERIVATIVE_ENABLED:
                    candidates.extend(self._expand_derivatives(candidates, mkt))

            seen, deduped = set(), []
            for c in candidates:
                s = c["vt_symbol"]
                if s not in seen:
                    seen.add(s)
                    deduped.append(c)
            candidates = deduped

            all_candidates.extend(candidates)
            logger.info(f"[Selector] {mkt} 选股完成: {len(candidates)} 个候选")
        logger.info(f"[Selector] 总计候选: {len(all_candidates)} 个")
        return all_candidates

    def _scan_anomalies(self, market: str) -> List[Dict]:
        if self.kp is None:
            return []
        pool = self.US_CORE_POOL if market == "US" else self.HK_CORE_POOL
        out = []
        for sym in pool:
            try:
                df = self.kp.get_for_scan(sym)
                if df is None or len(df) < 2:
                    continue
                c = df["close"].astype(float).values
                v = df["volume"].astype(float).values
                h = df["high"].astype(float).values
                l = df["low"].astype(float).values
                last, prev = c[-1], c[-2]
                avg20 = float(np.mean(v[-20:])) if len(v) >= 20 else v[-1]
                vr = v[-1] / avg20 if avg20 > 0 else 1.0
                chg = (last - prev) / prev if prev > 0 else 0.0
                amp = (h[-1] - l[-1]) / prev if prev > 0 else 0.0
                atype, score = "none", 50
                if vr >= self.VOLUME_SURGE_THRESHOLD:
                    atype, score = "volume_surge", 80
                elif abs(chg) >= self.PRICE_CHANGE_THRESHOLD:
                    atype, score = "price_breakout", 78
                elif amp >= self.AMPLITUDE_THRESHOLD:
                    atype, score = "amplitude", 72
                if atype != "none":
                    out.append({"vt_symbol": sym, "market": market,
                                "asset_type": "EQUITY", "anomaly_type": atype,
                                "source": "anomaly", "score": score,
                                "extra": {"vol_ratio": round(vr, 2),
                                          "change_pct": round(chg, 4),
                                          "amplitude": round(amp, 4)}})
            except Exception as e:
                logger.debug(f"[Selector] 异动跳过 {sym}: {e}")
        return out

    def _scan_ipo(self, market: str) -> List[Dict]:
        return [{"vt_symbol": "02261.SEHK", "market": "HK",
                 "asset_type": "IPO", "anomaly_type": "ipo_listing",
                 "source": "ipo", "score": 92,
                 "extra": {"ipo_name": "Sample IPO"}}]

    def _futu_code_to_vt(self, futu_code: str) -> str:
        """富途代码 'HK.12345' → '12345.SEHK'"""
        if not futu_code or "." not in futu_code:
            raise ValueError(f"非法富途代码: {futu_code!r}")
        prefix, num = futu_code.split(".", 1)
        if prefix == "HK":
            if not num.isdigit():
                raise ValueError(f"港股代码应为数字: {futu_code}")
            return f"{num}.SEHK"
        elif prefix == "US":
            return f"{num}.SMART"
        else:
            return futu_code

    def _expand_derivatives(self, candidates, market: str) -> List[Dict]:
        """通过 get_warrant 查询窝轮+牛熊证，按字符串类型分流"""
        if self.ctx is None:
            logger.error("[Selector] quote_ctx 未注入，无法查询衍生品")
            return []

        equity = [c for c in candidates if c["asset_type"] == "EQUITY"][:self.MAX_UNDERLYING_SCANNED]
        derivs = []

        for ec in equity:
            underlying = ec["vt_symbol"]
            code = underlying.split(".")[0]
            futu_stock = f"HK.{code}"

            try:
                # 尝试带筛选参数的调用，失败则回退到无参数
                try:
                    from futu import WarrantRequest, SortField
                    req = WarrantRequest()
                    req.sort_field = SortField.TURNOVER
                    req.num = self.MAX_WARRANTS_PER_STOCK + self.MAX_CBBS_PER_STOCK
                    ret, ls = self.ctx.get_warrant(futu_stock, req)
                except Exception:
                    ret, ls = self.ctx.get_warrant(futu_stock)

                if ret != RET_OK:
                    logger.warning(f"[Selector] {underlying} get_warrant 失败: {ls}")
                    continue

                # 兼容不同返回值格式
                if isinstance(ls, (list, tuple)):
                    warrant_data_list, last_page, all_count = ls
                else:
                    warrant_data_list = ls
                    last_page = False
                    all_count = 0

                if warrant_data_list is None or len(warrant_data_list) == 0:
                    logger.info(f"[Selector] {underlying} 无衍生品")
                    continue

                warrants_taken = 0
                cbbc_taken = 0

                for _, row in warrant_data_list.iterrows():
                    raw_code = str(row.get("stock") or "").strip()
                    wrt_type_raw = row.get("type")
                    if wrt_type_raw is None:
                        continue
                    # 统一转为大写字符串
                    wrt_type_str = str(wrt_type_raw).upper()

                    if not raw_code:
                        continue

                    # 转换为 vt_symbol
                    try:
                        if raw_code.startswith("HK."):
                            num = raw_code.split(".", 1)[1]
                            vt = f"{num}.SEHK"
                        else:
                            continue
                    except Exception:
                        continue

                    # 字符串分流
                    if wrt_type_str in ("CALL", "PUT"):
                        if warrants_taken >= self.MAX_WARRANTS_PER_STOCK:
                            continue
                        derivs.append({
                            "vt_symbol": vt, "market": "HK",
                            "asset_type": "WARRANT", "anomaly_type": "none",
                            "source": "warrant", "score": 75,
                            "extra": {"underlying": underlying, "wrt_type": wrt_type_str},
                        })
                        warrants_taken += 1
                    elif wrt_type_str in ("BULL", "BEAR"):
                        if cbbc_taken >= self.MAX_CBBS_PER_STOCK:
                            continue
                        derivs.append({
                            "vt_symbol": vt, "market": "HK",
                            "asset_type": "CBBC", "anomaly_type": "none",
                            "source": "cbbc", "score": 73,
                            "extra": {"underlying": underlying, "wrt_type": wrt_type_str},
                        })
                        cbbc_taken += 1
                    # 界内证或其他忽略

                logger.info(f"[Selector] {underlying} → 窝轮 {warrants_taken} 个, 牛熊证 {cbbc_taken} 个")

            except Exception as e:
                logger.error(f"[Selector] get_warrant 查询失败 {underlying}: {e}", exc_info=True)

        logger.info(f"[Selector] 衍生品扩展完成: {len(derivs)} 个真实合约")
        return derivs