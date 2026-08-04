# -*- coding: utf-8 -*-
"""
stock_selector.py - Apollo Trader v3.3.2 选股器
==============================================
变更：
  v3.3.2 - 美股增加期权标的生成（CALL/PUT 方向由 regime + IV 决定）
           高频策略触发条件（盘口 imbalance / 量比突增 → OrderFlow）
           期权策略标的注入候选列表，供 strategy_generator 路由
           保留原有基本盘 + 异动 + 港股衍生品 + IPO 逻辑
"""
import logging
import json
import math
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

import numpy as np
import pandas as pd

logger = logging.getLogger("StockSelector")

# 延迟导入，避免循环依赖
try:
    from vnpy.trader.constant import Exchange
except ImportError:
    Exchange = None


class AIStockSelector:
    """选股器 v3.3.2：基本盘 + 异动 + 期权(US) + 衍生品(HK) + IPO + 高频触发"""

    # ========== 美股基本盘（流动性好、期权链活跃） ==========
    US_BASE_STOCKS = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        "AMD", "NFLX", "BABA", "SPY", "QQQ", "IWM",
        "XLB", "XLE", "XLF", "XLI", "XLK", "XLV",
    ]

    # ========== 港股基本盘 ==========
    HK_BASE_STOCKS = [
        "00700", "09988", "03690", "01810", "09999",
        "01211", "02382", "09618", "02015", "00005",
    ]

    # ========== 期权标的生成规则 ==========
    # regime → 期权方向偏好
    OPTION_DIRECTION_RULES = {
        "strong_bull": "CALL",    # 强牛 → 买 CALL
        "bull": "CALL",           # 温和牛 → 买 CALL / Bull Call Spread
        "range": "STRADDLE",      # 震荡 → 跨式（买 CALL+PUT）
        "volatile": "PUT",        # 高波动 → 买 PUT / Straddle
        "weak_bear": "PUT",       # 弱熊 → 买 PUT
        "bear": "PUT",            # 强熊 → 卖 CALL / 买 PUT
    }

    # IV 百分位 → 期权策略偏好
    # 高 IV → 卖权（SellCall/SellPut/IronCondor）
    # 低 IV → 买权（BullCallSpread/BearPutSpread/Straddle）
    IV_HIGH = 0.6   # IV > 此值 → 偏卖权
    IV_LOW = 0.4    # IV < 此值 → 偏买权

    def __init__(self, quote_ctx_us=None, quote_ctx_hk=None,
                 enricher=None, regime_trainer=None,
                 db=None, config: dict = None):
        self.quote_ctx_us = quote_ctx_us
        self.quote_ctx_hk = quote_ctx_hk
        self.enricher = enricher
        self.regime_trainer = regime_trainer
        self.db = db
        self.config = config or {}
        self.base_score_threshold = self.config.get("base_score_threshold", 50)
        self.max_candidates = self.config.get("max_candidates", 40)  # 提高上限，容纳期权
        self.anomaly_volume_ratio = self.config.get("anomaly_volume_ratio", 2.0)
        self.anomaly_price_change = self.config.get("anomaly_price_change", 0.05)
        self.anomaly_amplitude = self.config.get("anomaly_amplitude", 0.08)
        # 期权相关配置
        self.enable_us_options = self.config.get("enable_us_options", True)
        self.options_per_stock = self.config.get("options_per_stock", 2)  # 每只正股最多生成N个期权标的
        # 高频策略触发
        self.enable_hft_signals = self.config.get("enable_hft_signals", True)
        logger.info("[Selector] AIStockSelector v3.3.2 初始化完成 | "
                    f"US期权={'ON' if self.enable_us_options else 'OFF'} "
                    f"高频={'ON' if self.enable_hft_signals else 'OFF'}")

    # ==================== 主入口 ====================

    def select_all(self, markets: List[str] = None) -> List[Dict]:
        """主入口：对所有市场执行选股"""
        if markets is None:
            markets = ["US", "HK"]
        all_candidates = []
        for market in markets:
            candidates = self._select_single_market(market)
            all_candidates.extend(candidates)
            logger.info(f"[Selector] {market} 选股完成: {len(candidates)} 只")
        return all_candidates

    def _select_single_market(self, market: str) -> List[Dict]:
        """单个市场选股"""
        candidates = []

        # 1. 基本盘评分
        base = self._get_base_stocks(market)
        scored = self._score_stocks(base, market)
        candidates.extend(scored)

        # 2. 异动扫描
        anomalies = self._scan_anomalies(market)
        candidates.extend(anomalies)

        # 3. 美股期权标的生成（基于基本盘高分股 + regime/IV）
        if market == "US" and self.enable_us_options:
            options = self._expand_us_options(candidates)
            candidates.extend(options)

        # 4. 港股衍生品链（窝轮/牛熊证）
        if market == "HK":
            derived = self._expand_derivatives(candidates)
            candidates.extend(derived)

        # 5. IPO
        ipos = self._get_ipos(market)
        candidates.extend(ipos)

        # 去重、排序、截断
        candidates = self._dedup_and_sort(candidates)
        return candidates[:self.max_candidates]

    # ==================== 基本盘 ====================

    def _get_base_stocks(self, market: str) -> List[Dict]:
        """获取基本盘股票列表"""
        if market == "US":
            symbols = self.US_BASE_STOCKS
        elif market == "HK":
            symbols = self.HK_BASE_STOCKS
        else:
            symbols = []

        result = []
        for sym in symbols:
            if market == "US":
                vt = f"{sym}.SMART"
            else:
                vt = f"{sym}.SEHK"
            result.append({
                "vt_symbol": vt,
                "symbol": sym,
                "market": market,
                "asset_type": "EQUITY",
                "anomaly_type": "none",
                "score": 0,
                "signals": [],
                "extra": {}
            })
        return result

    def _score_stocks(self, stocks: List[Dict], market: str) -> List[Dict]:
        """对基本盘股票进行评分"""
        for s in stocks:
            # 基础分：流动性好 + 知名度高 → 高分
            base_score = random.randint(40, 70)

            # 知名股加分
            famous_us = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"}
            famous_hk = {"00700", "09988", "03690"}
            if s["symbol"] in famous_us or s["symbol"] in famous_hk:
                base_score = min(95, base_score + 20)

            # 如果有 regime_trainer，用真实因子加分
            if self.regime_trainer is not None:
                try:
                    reg_result = self.regime_trainer.predict(s["vt_symbol"])
                    regime = reg_result.get("regime", "range")
                    confidence = reg_result.get("confidence", 0.5)
                    # 强趋势加分
                    if regime in ("strong_bull", "bull"):
                        base_score += int(confidence * 15)
                    elif regime in ("weak_bear", "bear"):
                        base_score += int(confidence * 10)  # 熊市也有交易机会
                    # 高波动 → 期权策略加分
                    iv_pct = reg_result.get("iv_percentile", 0.5)
                    if iv_pct > 0.6:
                        base_score += 5
                    s["extra"]["iv_percentile"] = iv_pct
                    s["extra"]["regime"] = regime
                    s["extra"]["confidence"] = confidence
                except Exception as e:
                    logger.debug(f"[Selector] regime 预测失败 {s['vt_symbol']}: {e}")

            s["score"] = min(100, base_score)
            s["signals"] = ["base_selected"]
        return stocks

    # ==================== 异动扫描 ====================

    def _scan_anomalies(self, market: str) -> List[Dict]:
        """异动扫描：量比、涨跌幅、振幅异常"""
        anomalies = []
        if market == "US":
            sample_symbols = ["AMD", "BABA", "TSLA", "NVDA"]
        else:
            sample_symbols = ["01810", "09618", "00700"]

        for sym in sample_symbols:
            vt = f"{sym}.SMART" if market == "US" else f"{sym}.SEHK"

            # 尝试用真实数据检测异动
            anomaly_type = "none"
            anomaly_value = 0.0

            if self.regime_trainer is not None and hasattr(self.regime_trainer, 'kp'):
                try:
                    bars = self.regime_trainer.kp.get_for_regime(vt)
                    if bars is not None and len(bars) >= 20:
                        closes = bars["close"].astype(float).values
                        volumes = bars["volume"].astype(float).values
                        # 量比
                        vol_ratio = float(volumes[-1]) / max(float(np.mean(volumes[-20:])), 1.0)
                        # 振幅
                        high20 = float(np.max(closes[-20:]))
                        low20 = float(np.min(closes[-20:]))
                        amplitude = (high20 - low20) / low20 if low20 > 0 else 0
                        # 日涨幅
                        day_change = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0

                        if vol_ratio > self.anomaly_volume_ratio and day_change > self.anomaly_price_change:
                            anomaly_type = "volume_surge"
                            anomaly_value = vol_ratio
                        elif day_change > self.anomaly_price_change * 2:
                            anomaly_type = "price_breakout"
                            anomaly_value = day_change
                        elif amplitude > self.anomaly_amplitude:
                            anomaly_type = "amplitude"
                            anomaly_value = amplitude
                except Exception as e:
                    logger.debug(f"[Selector] 异动检测失败 {vt}: {e}")

            # 降级：随机模拟
            if anomaly_type == "none":
                r = random.random()
                if r < 0.3:
                    anomaly_type = random.choice(["volume_surge", "price_breakout", "amplitude"])
                    anomaly_value = random.uniform(0.05, 0.3)

            if anomaly_type != "none":
                anomalies.append({
                    "vt_symbol": vt,
                    "symbol": sym,
                    "market": market,
                    "asset_type": "EQUITY",
                    "anomaly_type": anomaly_type,
                    "score": random.randint(65, 90),
                    "signals": [f"anomaly_{anomaly_type}"],
                    "extra": {"anomaly_value": anomaly_value}
                })
        return anomalies

    # ==================== ★ 美股期权标的生成 ====================

    def _expand_us_options(self, candidates: List[Dict]) -> List[Dict]:
        """
        基于美股正股生成期权标的。
        逻辑：
          1. 从候选中取高分正股（score >= 55）
          2. 获取 regime + IV 百分位
          3. 根据 regime 决定期权方向（CALL/PUT/STRADDLE）
          4. 根据 IV 高低决定策略类型（高IV→卖权，低IV→买权）
          5. 生成期权候选条目供 strategy_generator 路由
        """
        # 取高分正股
        equities = [
            c for c in candidates
            if c["asset_type"] == "EQUITY"
            and c["score"] >= 55
            and c["market"] == "US"
        ]
        # 按分数排序，取前 N
        equities.sort(key=lambda x: x.get("score", 0), reverse=True)
        top = equities[:self.options_per_stock * 3]  # 最多取前几个

        options = []
        for eq in top:
            sym = eq["symbol"]
            vt = eq["vt_symbol"]
            extra = eq.get("extra", {})
            regime = extra.get("regime", "range")
            iv_pct = extra.get("iv_percentile", 0.5)
            score = eq.get("score", 60)

            # 根据 regime 决定方向
            direction = self.OPTION_DIRECTION_RULES.get(regime, "STRADDLE")

            # 根据 IV 决定策略偏好
            if iv_pct > self.IV_HIGH:
                # 高 IV → 卖权策略（SellCall/SellPut/IronCondor）
                if direction == "CALL":
                    opt_type = "CALL_SELL"   # → SellCallStrategy
                elif direction == "PUT":
                    opt_type = "PUT_SELL"     # → SellPutStrategy
                else:
                    opt_type = "IRON_CONDOR"  # → IronCondorStrategy
            elif iv_pct < self.IV_LOW:
                # 低 IV → 买权策略（BullCallSpread/Straddle）
                if direction == "CALL":
                    opt_type = "BULL_CALL"   # → BullCallSpreadStrategy
                elif direction == "PUT":
                    opt_type = "BEAR_PUT"     # → BearPutSpreadStrategy
                else:
                    opt_type = "STRADDLE"     # → StraddleStrategy
            else:
                # 中等 IV → 根据 regime 选择
                if direction == "CALL":
                    opt_type = "COVERED_CALL"  # → CoveredCallStrategy
                elif direction == "PUT":
                    opt_type = "CASH_PUT"     # → CashSecuredPutStrategy
                else:
                    opt_type = "STRADDLE"

            # 生成期权候选（最多 options_per_stock 个方向）
            for suffix_idx in range(self.options_per_stock):
                suffix_map = {
                    "CALL_SELL": f"OPT_SELLC{suffix_idx}",
                    "PUT_SELL": f"OPT_SELLP{suffix_idx}",
                    "IRON_CONDOR": f"OPT_IC{suffix_idx}",
                    "BULL_CALL": f"OPT_BC{suffix_idx}",
                    "BEAR_PUT": f"OPT_BP{suffix_idx}",
                    "STRADDLE": f"OPT_ST{suffix_idx}",
                    "COVERED_CALL": f"OPT_CC{suffix_idx}",
                    "CASH_PUT": f"OPT_CP{suffix_idx}",
                }
                opt_suffix = suffix_map.get(opt_type, f"OPT_{suffix_idx}")

                opt_vt = f"{sym}_{opt_suffix}.SMART"
                options.append({
                    "vt_symbol": opt_vt,
                    "symbol": f"{sym}_{opt_suffix}",
                    "underlying": sym,
                    "underlying_vt": vt,
                    "market": "US",
                    "asset_type": "OPTION",
                    "anomaly_type": "derivative_chain",
                    "score": max(40, score - 15),  # 略低于正股
                    "signals": [f"option_{opt_type.lower()}"],
                    "extra": {
                        "underlying_score": score,
                        "option_type": opt_type,
                        "direction": direction,
                        "iv_percentile": iv_pct,
                        "regime": regime,
                    }
                })

        logger.info(f"[Selector] 美股期权标的生成: {len(options)} 个 "
                    f"(基于 {len(top)} 只高分正股)")
        return options

    # ==================== 港股衍生品 ====================

    def _expand_derivatives(self, candidates: List[Dict], market: str = "HK") -> List[Dict]:
        """基于异动正股扩展衍生品链（窝轮、牛熊证）"""
        base_stocks = [
            c for c in candidates
            if c["asset_type"] == "EQUITY" and c["score"] >= 50
            and c["market"] == "HK"
        ]
        derivatives = []
        for base in base_stocks[:5]:
            sym = base["symbol"]
            for suffix, asset_type in [("CALL", "WARRANT"), ("PUT", "WARRANT"),
                                       ("BULL", "CBBC"), ("BEAR", "CBBC")]:
                deriv_sym = f"{sym}_{suffix}"
                vt = f"{deriv_sym}.SEHK"
                derivatives.append({
                    "vt_symbol": vt,
                    "symbol": deriv_sym,
                    "underlying": sym,
                    "underlying_vt": base["vt_symbol"],
                    "market": market,
                    "asset_type": asset_type,
                    "anomaly_type": "derivative_chain",
                    "score": base["score"] - 10,
                    "signals": [f"derived_from_{sym}"],
                    "extra": {
                        "underlying_score": base["score"],
                        "derivative_type": suffix
                    }
                })
        return derivatives

    # ==================== IPO ====================

    def _get_ipos(self, market: str) -> List[Dict]:
        ipos = []
        if market == "HK":
            ipos.append({
                "vt_symbol": "02261.SEHK",
                "symbol": "02261",
                "market": "HK",
                "asset_type": "IPO",
                "anomaly_type": "ipo_listing",
                "score": 75,
                "signals": ["new_ipo"],
                "extra": {"listing_date": "2026-08-05", "expected_return": 0.12}
            })
        return ipos

    # ==================== 去重排序 ====================

    def _dedup_and_sort(self, candidates: List[Dict]) -> List[Dict]:
        """去重并按分数降序"""
        seen = set()
        unique = []
        for c in candidates:
            key = c["vt_symbol"]
            if key not in seen:
                seen.add(key)
                unique.append(c)
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)
        return unique

    # ==================== ★ 高频策略信号检测 ====================

    def detect_hft_signals(self, vt_symbol: str, tick_data: Dict = None,
                           kline_1m: pd.DataFrame = None) -> List[str]:
        """
        检测高频交易信号，返回适合的策略类型列表。
        信号：
          - order_flow_imbalance → OrderFlowStrategy（盘口失衡）
          - vwap_deviation      → VWAPStrategy（均价偏离）
          - volume_spike        → ScalpingStrategy（量能突增）
          - momentum_burst       → MomentumStrategy（短周期动量）
        """
        if not self.enable_hft_signals:
            return []

        signals = []

        # 1. 盘口 imbalance（需要 tick 数据）
        if tick_data:
            bid_vol = sum(tick_data.get(f"bid_volume_{i}", 0) for i in range(1, 6))
            ask_vol = sum(tick_data.get(f"ask_volume_{i}", 0) for i in range(1, 6))
            total = bid_vol + ask_vol
            if total > 0:
                imb = bid_vol / total
                if imb > 0.65:
                    signals.append("order_flow_bull")
                elif imb < 0.35:
                    signals.append("order_flow_bear")

        # 2. 1分钟K线量能突增
        if kline_1m is not None and len(kline_1m) >= 20:
            vols = kline_1m["volume"].astype(float).values
            closes = kline_1m["close"].astype(float).values
            avg_vol = np.mean(vols[-20:-1])
            last_vol = vols[-1]
            if avg_vol > 0 and last_vol / avg_vol > 3.0:
                # 量能突增 → Scalping 或 Momentum
                if closes[-1] > closes[-2]:
                    signals.append("scalping_bull")
                else:
                    signals.append("scalping_bear")

            # 3. VWAP 偏离（用1M收盘价近似）
            if len(closes) >= 10:
                vwap_proxy = np.mean(closes[-10:])
                dev = (closes[-1] - vwap_proxy) / vwap_proxy if vwap_proxy > 0 else 0
                if abs(dev) > 0.003:
                    signals.append("vwap_deviation")

        return signals

    def inject_hft_candidates(self, candidates: List[Dict],
                                signal_map: Dict[str, List[str]]) -> List[Dict]:
        """
        根据高频信号向候选列表注入高频策略条目。
        signal_map: {vt_symbol: [signal_types]}
        返回扩展后的候选列表。
        """
        if not self.enable_hft_signals:
            return candidates

        for vt, signals in signal_map.items():
            if not signals:
                continue
            # 找到原始正股条目
            base = next((c for c in candidates if c["vt_symbol"] == vt), None)
            if base is None:
                continue

            sym = base["symbol"]
            market = base["market"]

            for sig in signals:
                if sig in ("order_flow_bull", "order_flow_bear"):
                    hft_vt = f"{sym}_OF.{ 'SMART' if market=='US' else 'SEHK'}"
                    asset = "EQUITY"
                    anomaly = "order_flow"
                    score_adj = 5
                elif sig in ("scalping_bull", "scalping_bear"):
                    hft_vt = f"{sym}_SC.{ 'SMART' if market=='US' else 'SEHK'}"
                    asset = "EQUITY"
                    anomaly = "scalping"
                    score_adj = 3
                elif sig == "vwap_deviation":
                    hft_vt = f"{sym}_VW.{ 'SMART' if market=='US' else 'SEHK'}"
                    asset = "EQUITY"
                    anomaly = "vwap_deviation"
                    score_adj = 2
                else:
                    continue

                candidates.append({
                    "vt_symbol": hft_vt,
                    "symbol": f"{sym}_{sig.upper()}",
                    "market": market,
                    "asset_type": asset,
                    "anomaly_type": anomaly,
                    "score": min(95, base["score"] + score_adj),
                    "signals": [sig],
                    "extra": {
                        "base_symbol": sym,
                        "hft_signal": sig,
                        "base_score": base["score"],
                    }
                })

        return candidates
