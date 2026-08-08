# -*- coding: utf-8 -*-
"""
ai/stock_selector.py - 选股模块 v3.8.1 (Fixed: 过滤含点股票 + 无点不转换)
========================================
- 核心池（白名单）+ 富途 get_stock_filter 动态筛选
- 异动扫描 + IPO + 衍生品
- 返回标准 vt_symbol 格式：XXXX.SMART / XXXX.SEHK
- v3.8.1 修复：
  - 含点股票（WSO.B 等）在入口直接丢弃
  - 无点代码原样保留（AAPL → AAPL.SMART，不产生 AAPL_SMART）
  - 构造函数保持 quote_ctx= 参数（与 scheduler_jobs.py 调用一致）
- ★ v3.8.2 修复：
  - _normalize_vt_symbol 不再对已是标准 vt_symbol 的输入做二次转换
  - _is_valid_symbol 逻辑不变，注释更清晰
  - _dynamic_filter_us/_dynamic_filter_hk 符号拼接方式微调
"""
import logging
import numpy as np
import time
from typing import List, Dict, Optional

from core.kline_provider import KlineProvider

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

logger = logging.getLogger("StockSelector")


class StockSelector:
    """选股器 v3.8.2 (Fixed normalize)"""

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

    CALL = 1
    PUT = 2
    BULL = 3
    BEAR = 4

    def __init__(self, quote_ctx=None, db=None,
                 kline_provider: Optional[KlineProvider] = None,
                 config: Optional[dict] = None):
        self.ctx = quote_ctx
        self.db = db
        self.kp = kline_provider
        self.config = config or {}
        logger.info("[Selector] v3.8.2-fixed 初始化完成（过滤含点股票）")

    # ==================== 过滤与标准化 ====================
    def _is_valid_symbol(self, raw_symbol: str) -> bool:
        """
        过滤含点股票代码（WSO.B / BRK.B）。
        规则：去掉前缀和后缀后，纯代码如果还含点 → 丢弃。
        
        注意：AAPL.SMART 去掉后缀后是 AAPL（不含点）→ 保留 ✓
             WSO.B.SMART 去掉后缀后是 WSO.B（含点）→ 丢弃 ✓
        """
        if not raw_symbol:
            return False
        s = raw_symbol
        # 去掉市场前缀
        if s.startswith("US."):
            s = s[3:]
        elif s.startswith("HK."):
            s = s[3:]
        # 去掉交易所后缀
        if s.endswith(".SMART"):
            s = s[:-6]
        elif s.endswith(".SEHK"):
            s = s[:-5]
        # 此时 s 应该是纯代码
        if "." in s:
            logger.debug(f"[Selector] 丢弃含点股票: {raw_symbol}")
            return False
        return True

    def _normalize_vt_symbol(self, raw_symbol: str, market: str) -> str:
        """
        标准化 vt_symbol。
        ★ v3.8.2: 输入已经是标准 vt_symbol 格式（AAPL.SMART / 00700.SEHK），
          本方法只做一件事：确保后缀正确。
        
        规则：
        - AAPL.SMART   → AAPL.SMART（已经是正确格式，原样返回）
        - 00700.SEHK   → 00700.SEHK（原样返回）
        - AAPL          → AAPL.SMART（纯代码补后缀）
        - 00700         → 00700.SEHK
        - WSO.B.SMART   → WSO.B.SMART（含点但已是正确格式）
        - WSO.B         → WSO.B.SMART（补后缀）
        """
        if not raw_symbol:
            return raw_symbol
        
        # 已经是标准 vt_symbol 格式（含 .SMART 或 .SEHK 后缀），直接返回
        if raw_symbol.endswith(".SMART") or raw_symbol.endswith(".SEHK"):
            return raw_symbol
        
        # 去掉可能的前缀（富途返回的 US./HK.）
        if raw_symbol.startswith("US."):
            raw_symbol = raw_symbol[3:]
        elif raw_symbol.startswith("HK."):
            raw_symbol = raw_symbol[3:]
        
        # 此时 raw_symbol 应该是纯代码（AAPL / 00700 / WSO.B）
        # 含点的纯代码（WSO.B）保持原样，后面拼接后缀
        if market == "US":
            return f"{raw_symbol}.SMART"
        elif market == "HK":
            return f"{raw_symbol}.SEHK"
        else:
            return f"{raw_symbol}.{market}"

    # ==================== 主入口 ====================
    def run(self, markets: List[str] = None) -> List[Dict]:
        if markets is None:
            markets = ["US", "HK"]
        all_candidates = []
        for mkt in markets:
            logger.info(f"[Selector] === 开始选股: {mkt} ===")
            candidates = []

            if mkt == "US":
                # Layer 1: 核心池（白名单，优先级最高）
                for sym in self.US_CORE_POOL:
                    if not self._is_valid_symbol(sym):
                        continue
                    candidates.append({
                        "vt_symbol": sym, "market": mkt,
                        "asset_type": "EQUITY", "anomaly_type": "none",
                        "source": "core_pool", "score": 90, "extra": {},
                        "strategy_fit": self._determine_strategy_fit(sym, mkt),
                    })
                # Layer 2: 富途动态筛选
                candidates.extend(self._dynamic_filter_us(max_count=30))
            else:
                for sym in self.HK_CORE_POOL:
                    if not self._is_valid_symbol(sym):
                        continue
                    candidates.append({
                        "vt_symbol": sym, "market": mkt,
                        "asset_type": "EQUITY", "anomaly_type": "none",
                        "source": "core_pool", "score": 90, "extra": {},
                        "strategy_fit": self._determine_strategy_fit(sym, mkt),
                    })
                # Layer 2: 富途动态筛选港股
                candidates.extend(self._dynamic_filter_hk(max_count=20))

            candidates.extend(self._scan_anomalies(mkt))

            if mkt == "HK":
                candidates.extend(self._scan_ipo(mkt))
                if self.DERIVATIVE_ENABLED:
                    candidates.extend(self._expand_derivatives(candidates, mkt))
            else:
                candidates.extend(self._scan_hft_pool())

            # 去重（保留首次出现的，即核心池优先）
            seen, deduped = set(), []
            for c in candidates:
                s = c["vt_symbol"]
                if s not in seen:
                    seen.add(s)
                    deduped.append(c)
            candidates = deduped

            # 兜底过滤：再次确认无含点股票漏网
            candidates = [c for c in candidates if self._is_valid_symbol(c["vt_symbol"])]

            # ★ v3.8.2: 标准化（对已是标准 vt_symbol 的不会改变）
            for c in candidates:
                c["vt_symbol"] = self._normalize_vt_symbol(c["vt_symbol"], c["market"])

            all_candidates.extend(candidates)
            logger.info(f"[Selector] {mkt} 选股完成: {len(candidates)} 个候选")
        logger.info(f"[Selector] 总计候选: {len(all_candidates)} 个")
        return all_candidates

    def select_all(self, markets: List[str] = None) -> List[Dict]:
        """兼容接口"""
        return self.run(markets)

    def _determine_strategy_fit(self, symbol: str, market: str) -> list:
        fits = ["equity"]
        if market == "US":
            fits.append("option")
            fits.append("hft")
        else:
            fits.append("option")
            if symbol in ("00700.SEHK", "09988.SEHK", "03690.SEHK"):
                fits.append("hft")
        return fits

    # ==================== 动态筛选美股 ====================
    def _dynamic_filter_us(self, max_count: int = 30) -> List[Dict]:
        """通过富途 get_stock_filter 动态筛选美股"""
        if self.ctx is None:
            logger.warning("[Selector] quote_ctx 未注入，跳过动态筛选")
            return []

        try:
            from futu import SimpleFilter, AccumulateFilter, StockField, SortDir, Market

            cap_filter = SimpleFilter()
            cap_filter.stock_field = StockField.MARKET_VAL
            cap_filter.filter_min = 100e8
            cap_filter.is_no_filter = False
            cap_filter.sort = SortDir.DESCEND

            price_filter = SimpleFilter()
            price_filter.stock_field = StockField.CUR_PRICE
            price_filter.filter_min = 10.0
            price_filter.filter_max = 1000.0
            price_filter.is_no_filter = False

            pe_filter = SimpleFilter()
            pe_filter.stock_field = StockField.PE_TTM
            pe_filter.filter_min = 10.0
            pe_filter.filter_max = 40.0
            pe_filter.is_no_filter = False

            vol_filter = SimpleFilter()
            vol_filter.stock_field = StockField.VOLUME_RATIO
            vol_filter.filter_min = 1.2
            vol_filter.is_no_filter = False

            change_filter = AccumulateFilter()
            change_filter.stock_field = StockField.CHANGE_RATE
            change_filter.filter_min = 0.0
            change_filter.filter_max = 5.0
            change_filter.days = 1
            change_filter.is_no_filter = False

            filter_list = [cap_filter, price_filter, pe_filter, vol_filter, change_filter]
            results = []

            for plate in ["US.NASDAQ", "US.NYSE"]:
                begin = 0
                while len(results) < max_count:
                    ret, ls = self.ctx.get_stock_filter(
                        market=Market.US,
                        filter_list=filter_list,
                        plate_code=plate,
                        begin=begin,
                        num=min(max_count - len(results), 200)
                    )
                    if ret != RET_OK:
                        logger.warning(f"[Selector] 动态筛选 {plate} 失败: {ls}")
                        break

                    last_page, all_count, ret_list = ls
                    for item in ret_list:
                        code = item.stock_code  # "US.AAPL" 或 "US.WSO.B"
                        # ★ 提前过滤含点股票
                        pure = code[3:] if code.startswith("US.") else code
                        if "." in pure:
                            logger.debug(f"[Selector] 动态筛选丢弃含点股票: {code}")
                            continue
                        # pure 是无点纯代码，直接拼后缀
                        sym = f"{pure}.SMART"
                        if any(c["vt_symbol"] == sym for c in results):
                            continue
                        results.append({
                            "vt_symbol": sym, "market": "US",
                            "asset_type": "EQUITY", "anomaly_type": "none",
                            "source": f"dynamic_{plate}", "score": 80,
                            "extra": {
                                "cur_price": item[price_filter],
                                "market_val": item[cap_filter],
                                "pe_ttm": item[pe_filter],
                                "volume_ratio": item[vol_filter],
                                "change_rate": item[change_filter],
                            },
                            "strategy_fit": self._determine_strategy_fit(sym, "US"),
                        })
                        if len(results) >= max_count:
                            break

                    if last_page:
                        break
                    begin += len(ret_list)
                    time.sleep(3)

            logger.info(f"[Selector] 动态筛选美股得到 {len(results)} 只")
            return results
        except Exception as e:
            logger.error(f"[Selector] get_stock_filter 异常: {e}", exc_info=True)
            return []

    # ==================== 动态筛选港股 ====================
    def _dynamic_filter_hk(self, max_count: int = 20) -> List[Dict]:
        """通过富途 get_stock_filter 动态筛选港股"""
        if self.ctx is None:
            logger.warning("[Selector] quote_ctx 未注入，跳过港股动态筛选")
            return []

        try:
            from futu import SimpleFilter, StockField, SortDir, Market

            cap_filter = SimpleFilter()
            cap_filter.stock_field = StockField.MARKET_VAL
            cap_filter.filter_min = 50e8
            cap_filter.is_no_filter = False
            cap_filter.sort = SortDir.DESCEND

            price_filter = SimpleFilter()
            price_filter.stock_field = StockField.CUR_PRICE
            price_filter.filter_min = 5.0
            price_filter.filter_max = 500.0
            price_filter.is_no_filter = False

            vol_filter = SimpleFilter()
            vol_filter.stock_field = StockField.VOLUME_RATIO
            vol_filter.filter_min = 1.2
            vol_filter.is_no_filter = False

            filter_list = [cap_filter, price_filter, vol_filter]
            results = []

            plates = ["HK.Motherboard", None]
            for plate in plates:
                if len(results) >= max_count:
                    break
                begin = 0
                while len(results) < max_count:
                    ret, ls = self.ctx.get_stock_filter(
                        market=Market.HK,
                        filter_list=filter_list,
                        plate_code=plate,
                        begin=begin,
                        num=min(max_count - len(results), 200)
                    )
                    if ret != RET_OK:
                        logger.warning(f"[Selector] 港股动态筛选 {plate} 失败: {ls}")
                        break

                    last_page, all_count, ret_list = ls
                    for item in ret_list:
                        code = item.stock_code  # "HK.00700"
                        pure = code[3:] if code.startswith("HK.") else code
                        if "." in pure:
                            logger.debug(f"[Selector] 港股动态筛选丢弃含点股票: {code}")
                            continue
                        sym = f"{pure}.SEHK"
                        if any(c["vt_symbol"] == sym for c in results):
                            continue
                        if sym in self.HK_CORE_POOL:
                            continue
                        results.append({
                            "vt_symbol": sym, "market": "HK",
                            "asset_type": "EQUITY", "anomaly_type": "none",
                            "source": f"hk_dynamic_{plate}",
                            "score": 80,
                            "extra": {
                                "cur_price": item[price_filter],
                                "market_val": item[cap_filter],
                                "volume_ratio": item[vol_filter],
                            },
                            "strategy_fit": self._determine_strategy_fit(sym, "HK"),
                        })
                        if len(results) >= max_count:
                            break

                    if last_page:
                        break
                    begin += len(ret_list)
                    time.sleep(3)

            logger.info(f"[Selector] 动态筛选港股得到 {len(results)} 只")
            return results
        except Exception as e:
            logger.error(f"[Selector] 港股动态筛选异常: {e}", exc_info=True)
            return []

    # ==================== 异动扫描 ====================
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
                                          "amplitude": round(amp, 4)},
                                "strategy_fit": self._determine_strategy_fit(sym, market)})
            except Exception as e:
                logger.debug(f"[Selector] 异动跳过 {sym}: {e}")
        return out

    def _scan_ipo(self, market: str) -> List[Dict]:
        return [{"vt_symbol": "02261.SEHK", "market": "HK",
                 "asset_type": "IPO", "anomaly_type": "ipo_listing",
                 "source": "ipo", "score": 92,
                 "extra": {"ipo_name": "Sample IPO"},
                 "strategy_fit": ["equity"]}]

    def _scan_hft_pool(self) -> List[Dict]:
        hft_symbols = ["SPY.SMART", "QQQ.SMART", "AAPL.SMART", "MSFT.SMART", "AMZN.SMART"]
        out = []
        for sym in hft_symbols:
            if not self._is_valid_symbol(sym):
                continue
            out.append({
                "vt_symbol": sym, "market": "US",
                "asset_type": "EQUITY", "anomaly_type": "none",
                "source": "hft_pool", "score": 70,
                "extra": {"hft_flag": True},
                "strategy_fit": ["hft"],
            })
        return out

    def _futu_code_to_vt(self, futu_code: str) -> str:
        if not futu_code or "." not in futu_code:
            raise ValueError(f"非法富途代码: {futu_code!r}")
        prefix, num = futu_code.split(".", 1)
        if prefix == "HK":
            if not num.isdigit():
                raise ValueError(f"港股代码应为数字: {futu_code}")
            return f"{num}.SEHK"
        elif prefix == "US":
            # 美股代码可能包含点，需替换
            safe_num = num.replace(".", "_")
            return f"{safe_num}.SMART"
        else:
            return futu_code

    # ==================== 衍生品扩展 ====================
    def _expand_derivatives(self, candidates, market: str) -> List[Dict]:
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
                    wrt_type_str = str(wrt_type_raw).upper()

                    if not raw_code:
                        continue

                    # ★ 过滤含点衍生品代码
                    if "." in raw_code.replace("HK.", ""):
                        logger.debug(f"[Selector] 丢弃含点衍生品: {raw_code}")
                        continue

                    try:
                        if raw_code.startswith("HK."):
                            num = raw_code.split(".", 1)[1]
                            vt = f"{num}.SEHK"
                        else:
                            continue
                    except Exception:
                        continue

                    if wrt_type_str in ("CALL", "PUT"):
                        if warrants_taken >= self.MAX_WARRANTS_PER_STOCK:
                            continue
                        derivs.append({
                            "vt_symbol": vt, "market": "HK",
                            "asset_type": "WARRANT", "anomaly_type": "none",
                            "source": "warrant", "score": 75,
                            "extra": {"underlying": underlying, "wrt_type": wrt_type_str},
                            "strategy_fit": ["equity"],
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
                            "strategy_fit": ["equity"],
                        })
                        cbbc_taken += 1

                logger.info(f"[Selector] {underlying} → 窝轮 {warrants_taken} 个, 牛熊证 {cbbc_taken} 个")

            except Exception as e:
                logger.error(f"[Selector] get_warrant 查询失败 {underlying}: {e}", exc_info=True)

        logger.info(f"[Selector] 衍生品扩展完成: {len(derivs)} 个真实合约")
        return derivs