"""
ai/stock_selector.py — AI 选股器 v3.3.1
修复：__init__ 兼容旧版 quote_ctx 参数；去掉美股IPO扫描
"""
import logging
import time
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any

import pandas as pd
import numpy as np

from core.regime_trainer import RegimeTrainer
from core.futu_data_enricher import FutuDataEnricher

log = logging.getLogger("StockSelector")


class AIStockSelector:
    """AI 选股器：融合异动扫描 + 基本盘评分 + 衍生品链 + IPO"""

    def __init__(self,
                 quote_ctx_us: Optional[object] = None,
                 quote_ctx_hk: Optional[object] = None,
                 enricher: Optional[FutuDataEnricher] = None,
                 regime_trainer: Optional[RegimeTrainer] = None,
                 db: Optional[object] = None,
                 config: Optional[dict] = None,
                 quote_ctx: Optional[object] = None):   # ← 兼容旧版调用
        """
        :param quote_ctx_us: 富途美股行情上下文
        :param quote_ctx_hk: 富途港股行情上下文
        :param enricher: 数据增强器
        :param regime_trainer: 市场状态训练器
        :param db: DBManager 实例
        :param config: 配置字典
        :param quote_ctx: 兼容旧版单一上下文（自动设为 quote_ctx_us）
        """
        self.ctx_us = quote_ctx_us or quote_ctx
        self.ctx_hk = quote_ctx_hk
        self.enricher = enricher
        self.regime_trainer = regime_trainer
        self.db = db
        self.config = config or {}

        # 基本盘股票池（可配置）
        self.base_pool_us = self.config.get("base_pool_us", [
            "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AMD","NFLX","BABA",
            "SPY","QQQ","DIA","IWM","XLE","XLF","XLK","XLV","XLI","XLB"
        ])
        self.base_pool_hk = self.config.get("base_pool_hk", [
            "00700","09988","03690","01810","09999","01211","02382","09618","02015","09888"
        ])

        # 异动扫描缓存
        self._last_scan = 0.0
        self._scan_cache: List[dict] = []

    # ========== 对外主接口 ==========
    def select_all(self, markets: List[str] = None) -> List[dict]:
        """执行全量选股：异动扫描 + 基本盘 + 衍生品 + IPO"""
        if markets is None:
            markets = ["US", "HK"]

        all_candidates = []

        for m in markets:
            # 1. 基本盘
            base = self._select_base(m)
            all_candidates.extend(base)

            # 2. 异动扫描（量比/涨幅/振幅）
            anomaly = self._scan_anomaly(m)
            all_candidates.extend(anomaly)

            # 3. IPO（只保留港股IPO）
            if m == "HK":   # ← 去掉美股IPO
                ipo = self._scan_ipo(m)
                all_candidates.extend(ipo)

            # 4. 衍生品链（港股正股异动扩展窝轮/牛熊）
            if m == "HK":
                deriv = self._expand_derivatives(anomaly)
                all_candidates.extend(deriv)

        # 去重（按 vt_symbol 去重，保留第一个出现的）
        seen = set()
        unique = []
        for c in all_candidates:
            vt = c.get("vt_symbol", "")
            if vt not in seen:
                seen.add(vt)
                unique.append(c)

        log.info(f"[Selector] ✅ 合并候选: {len(unique)} 只 "
                 f"(正股={sum(1 for c in unique if c.get('asset_type')=='EQUITY')}, "
                 f"衍生品={sum(1 for c in unique if c.get('asset_type') in ('WARRANT','CBBC','OPTION'))}, "
                 f"IPO={sum(1 for c in unique if c.get('asset_type')=='IPO')})")
        return unique

    # ========== 基本盘 ==========
    def _select_base(self, market: str) -> List[dict]:
        pool = self.base_pool_us if market == "US" else self.base_pool_hk
        results = []
        for sym in pool:
            vt = f"{sym}.SMART" if market == "US" else f"{sym}.SEHK"
            results.append({
                "vt_symbol": vt,
                "asset_type": "EQUITY",
                "anomaly_type": "none",
                "score": 50.0,
                "signals": ["基本盘"],
                "market": market,
                "underlying": "",
                "extra": {},
            })
        log.info(f"[Selector] 📊 基本盘评分: {len(results)} 只 ({market})")
        return results

    # ========== 异动扫描 ==========
    def _scan_anomaly(self, market: str) -> List[dict]:
        """使用富途 get_stock_filter 扫描异动（量比≥2x / 涨幅≥3% / 振幅≥4%）"""
        ctx = self.ctx_us if market == "US" else self.ctx_hk
        if ctx is None:
            return []

        try:
            from futu import (
                RET_OK, Market, AccumulateField, SortDir, AccumulateFilter
            )
        except ImportError:
            return []

        futu_market = Market.US if market == "US" else Market.HK
        results = []
        conditions = [
            ("volume_surge", AccumulateField.VOLUME, 2.0, 1e9, "量比"),
            ("price_breakout", AccumulateField.CHANGE_RATE, 3.0, 30.0, "涨幅"),
            ("amplitude", AccumulateField.AMPLITUDE, 4.0, 100.0, "振幅"),
        ]

        for anom_type, field, fmin, fmax, label in conditions:
            try:
                flt = AccumulateFilter(
                    stock_field=field,
                    filter_min=fmin,
                    filter_max=fmax,
                    days=1,
                    sort=SortDir.DESCEND,
                )
                ret, data = ctx.get_stock_filter(futu_market, [flt], begin=0, num=20)
                if ret != RET_OK or data is None or data.empty:
                    continue
                for _, r in data.iterrows():
                    code = str(r.get("code", ""))
                    if not code:
                        continue
                    sym = code.split(".", 1)[-1] if "." in code else code
                    vt = f"{sym}.SMART" if market == "US" else f"{sym}.SEHK"
                    val = float(r.get(label.lower(), 0))
                    score = min(val * 10, 100) if anom_type == "volume_surge" else min(50 + val * 3, 100)
                    results.append({
                        "vt_symbol": vt,
                        "asset_type": "EQUITY",
                        "anomaly_type": anom_type,
                        "score": round(score, 1),
                        "signals": [f"{label}{val:.1f}"],
                        "market": market,
                        "underlying": "",
                        "extra": {label.lower(): val},
                    })
            except Exception as e:
                log.debug(f"[Selector] {market} {anom_type} 扫描失败: {e}")

        log.info(f"[Selector] 📡 {market} 异动扫描: {len(results)} 只")
        return results

    # ========== IPO（仅港股） ==========
    def _scan_ipo(self, market: str) -> List[dict]:
        """扫描近期 IPO 新股（港股：最近14天）"""
        ctx = self.ctx_hk if market == "HK" else self.ctx_us
        if ctx is None:
            return []
        try:
            from futu import RET_OK, Market
        except ImportError:
            return []

        futu_market = Market.HK if market == "HK" else Market.US
        try:
            ret, data = ctx.get_ipo_list(futu_market)
            if ret != RET_OK or data is None or data.empty:
                return []
            now = datetime.now()
            cutoff = now - timedelta(days=14 if market == "HK" else 7)
            results = []
            for _, r in data.iterrows():
                list_time_str = str(r.get("list_time", ""))[:10]
                try:
                    list_time = datetime.strptime(list_time_str, "%Y-%m-%d")
                except:
                    continue
                if list_time < cutoff:
                    continue
                code = str(r.get("code", ""))
                if not code:
                    continue
                sym = code.split(".", 1)[-1] if "." in code else code
                vt = f"{sym}.SEHK" if market == "HK" else f"{sym}.SMART"
                results.append({
                    "vt_symbol": vt,
                    "asset_type": "IPO",
                    "anomaly_type": "ipo_listing",
                    "score": 80.0,
                    "signals": [f"IPO {r.get('name','')} {list_time_str}"],
                    "market": market,
                    "underlying": "",
                    "extra": {
                        "name": str(r.get("name","")),
                        "list_time": list_time_str,
                        "ipo_price_min": float(r.get("ipo_price_min",0) or 0),
                        "ipo_price_max": float(r.get("ipo_price_max",0) or 0),
                    },
                })
            log.info(f"[Selector] 🆕 {market} IPO 新股: {len(results)} 只")
            return results
        except Exception as e:
            log.debug(f"[Selector] {market} IPO扫描失败: {e}")
            return []

    # ========== 衍生品链扩展（港股窝轮/牛熊证） ==========
    def _expand_derivatives(self, anomaly_candidates: List[dict]) -> List[dict]:
        """对异动正股扩展窝轮/牛熊证链"""
        ctx = self.ctx_hk
        if ctx is None:
            return []
        try:
            from futu import RET_OK, WarrantMarket, WarrantScreenRequest, WarrantType
        except ImportError:
            return []

        results = []
        for cand in anomaly_candidates:
            if cand.get("market") != "HK":
                continue
            if cand.get("score", 0) < 60:
                continue
            vt = cand.get("vt_symbol", "")
            sym = vt.split(".")[0]
            futu_code = f"HK.{sym}"

            for wrt_type, asset_label in [(WarrantType.CALL, "WARRANT"),
                                           (WarrantType.PUT, "WARRANT"),
                                           (WarrantType.BULL, "CBBC"),
                                           (WarrantType.BEAR, "CBBC")]:
                try:
                    req = WarrantScreenRequest()
                    req.warrant_market = WarrantMarket.HK
                    req.warrant_type = wrt_type
                    req.code = futu_code
                    req.volume_min = 500000
                    req.leverage_min = 3.0
                    req.recovery_ratio_min = 5.0
                    ret, data = ctx.get_warrant_screen(req)
                    if ret != RET_OK or data is None or data.empty:
                        continue
                    for _, r in data.head(3).iterrows():
                        wcode = str(r.get("stock", ""))
                        if not wcode:
                            continue
                        wsym = wcode.split(".", 1)[-1] if "." in wcode else wcode
                        wvt = f"{wsym}.SEHK"
                        results.append({
                            "vt_symbol": wvt,
                            "asset_type": asset_label,
                            "anomaly_type": "derivative_chain",
                            "score": float(r.get("score", 60)),
                            "signals": [f"{asset_label} {r.get('name','')}"],
                            "market": "HK",
                            "underlying": vt,
                            "extra": {
                                "leverage": float(r.get("leverage",0)),
                                "delta": float(r.get("delta",0)),
                                "recovery_ratio": float(r.get("price_recovery_ratio",0)),
                                "expiry_days": int(r.get("expiry_date_distance",0)),
                            },
                        })
                except Exception as e:
                    log.debug(f"[Selector] 衍生品链 {wrt_type} 查询失败: {e}")
        log.info(f"[Selector] 🔗 HK 衍生品扩展: +{len(results)} 只")
        return results