# -*- coding: utf-8 -*-
"""
strategy_generator.py - v3.8.1 整合版（修复 KeyError: 'HK'）
"""
import json
import logging
import re
import threading
from typing import List, Dict, Optional, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from ai.param_advisor import ParamAdvisor
from core.regime_predictor import AdaptiveRegimePredictor

logger = logging.getLogger("StrategyGenerator")


class StrategyGenerator:
    """策略生成器 v3.8.1"""

    REGIME_NORMALIZATION = {
        "range_mid": "range", "range_low": "range", "range_high": "range",
        "up_mid": "bull", "up_high": "strong_bull", "up_low": "bull",
        "down_mid": "weak_bear", "down_high": "bear", "down_low": "weak_bear",
        "neutral": "range", "range_high_iv": "range", "volatile_low_iv": "volatile",
        "strong_bull": "strong_bull", "bull": "bull", "range": "range",
        "volatile": "volatile", "weak_bear": "weak_bear", "bear": "bear",
    }

    ROUTE_TABLE = {
        ("EQUITY", "none", "strong_bull"): "TrendStrategy",
        ("EQUITY", "none", "bull"): "TrendStrategy",
        ("EQUITY", "none", "range"): "GridStrategy",
        ("EQUITY", "none", "volatile"): "DualThrustStrategy",
        ("EQUITY", "none", "weak_bear"): "MomentumStrategy",
        ("EQUITY", "none", "bear"): "DualThrustStrategy",
        ("EQUITY", "volume_surge", "*"): "TrendStrategy",
        ("EQUITY", "price_breakout", "*"): "DualThrustStrategy",
        ("EQUITY", "amplitude", "*"): "ScalpingStrategy",
        ("IPO", "*", "*"): "IPOStrategy",
        ("WARRANT", "*", "*"): "WarrantStrategy",
        ("CBBC", "*", "*"): "CBBCStrategy",
        ("OPTION", "*", "strong_bull"): "SellPutStrategy",
        ("OPTION", "*", "bull"): "SellPutStrategy",
        ("OPTION", "*", "range"): "IronCondorStrategy",
        ("OPTION", "*", "volatile"): "IronCondorStrategy",
        ("OPTION", "*", "weak_bear"): "SellCallStrategy",
        ("OPTION", "*", "bear"): "SellCallStrategy",
    }

    FALLBACK_STRATEGIES = {
        "EQUITY": "GridStrategy", "IPO": "IPOStrategy",
        "WARRANT": "WarrantStrategy", "CBBC": "CBBCStrategy",
        "OPTION": "IronCondorStrategy",
    }

    DIAGNOSIS_REGIME_RULES = [
        (r"(多头排列|强势突破|52周高位.*9[5-9]%|52周高位.*100%)", "strong_bull"),
        (r"(站上MA20|周线偏多|均线多头|金叉)", "bull"),
        (r"(空头排列|跌破MA20|死叉|52周低位)", "weak_bear"),
        (r"(大幅震荡|高波动|振幅扩大)", "volatile"),
    ]

    HK_DISALLOWED_OPTION_STRATEGIES = {
        "SellPutStrategy", "SellCallStrategy", "CoveredCallStrategy",
        "BullCallSpreadStrategy", "BearPutSpreadStrategy",
        "IronCondorStrategy", "StraddleStrategy", "CashSecuredPutStrategy",
    }

    def __init__(self, quote_ctx=None, db_path: str = "data/history.db",
                 kline_provider=None, regime_predictor: AdaptiveRegimePredictor = None,
                 param_advisor: ParamAdvisor = None,
                 db=None, config: dict = None):
        self.ctx = quote_ctx
        self.db_path = db_path
        self.kp = kline_provider
        self.regime_predictor = regime_predictor
        self.param_advisor = param_advisor or ParamAdvisor(db)
        self.db = db
        self.config = config or {}
        self.gen_workers = self.config.get("gen_workers", 5)
        self._market_seen: Dict[str, set] = {}
        logger.info(f"[StrategyGenerator] v3.8.1 初始化完成 (workers={self.gen_workers})")

    def generate(self, candidates: List[Dict], regime_map: Dict[str, dict] = None,
                 market: str = "US") -> int:
        if not candidates:
            logger.warning("[Gen] 候选列表为空")
            return 0

        symbol_underlying_map = {}
        for c in candidates:
            vt = c.get("vt_symbol", "")
            extra = c.get("extra", {})
            ul = extra.get("underlying", None)
            if vt and ul:
                symbol_underlying_map[vt] = ul

        if regime_map is None:
            symbols = [c["vt_symbol"] for c in candidates if c.get("vt_symbol")]
            if self.regime_predictor:
                if hasattr(self.regime_predictor, 'batch_compute'):
                    regime_map = self.regime_predictor.batch_compute(
                        symbols, market=market, underlying_map=symbol_underlying_map
                    )
                else:
                    regime_map = {}
                    for s in symbols:
                        try:
                            ul = symbol_underlying_map.get(s)
                            result = self.regime_predictor.predict(s, market=market, underlying_symbol=ul)
                            regime_map[s] = {
                                "regime": result.get("regime", "range"),
                                "confidence": result.get("confidence", 0.5),
                                "iv_percentile": result.get("iv_percentile", 0.5),
                            }
                        except:
                            regime_map[s] = {"regime": "range", "confidence": 0.5, "iv_percentile": 0.5}
            else:
                regime_map = {s: {"regime": "range", "confidence": 0.5, "iv_percentile": 0.5} for s in symbols}

        # 归一化
        for k in list(regime_map.keys()):
            orig = regime_map[k].get("regime", "range")
            normalized = self._normalize_regime(orig)
            if orig != normalized:
                logger.debug(f"[Gen] regime 归一化: {orig} → {normalized} ({k})")
                regime_map[k]["regime"] = normalized

        # ★ 修复：初始化所有候选市场的去重集合
        if market not in self._market_seen:
            self._market_seen[market] = set()
        markets_in_candidates = set(c.get("market", market) for c in candidates)
        for m in markets_in_candidates:
            if m not in self._market_seen:
                self._market_seen[m] = set()

        written = 0
        skipped = 0
        write_lock = threading.Lock()

        def _process_one(cand):
            nonlocal written, skipped
            try:
                vt_symbol = cand["vt_symbol"]
                asset_type = cand.get("asset_type", "EQUITY")
                anomaly_type = cand.get("anomaly_type", "none")
                mkt = cand.get("market", market)
                strategy_fit = cand.get("strategy_fit", ["equity"])

                regime_info = regime_map.get(vt_symbol, {})
                base_regime = regime_info.get("regime", "range")
                iv_pct = regime_info.get("iv_percentile", 0.5)

                extra = cand.get("extra", {})
                diagnosis_text = extra.get("diagnosis", "")
                adjusted_regime = self._adjust_regime_by_diagnosis(base_regime, diagnosis_text)
                final_regime = self._normalize_regime(adjusted_regime) if adjusted_regime else self._normalize_regime(base_regime)

                if asset_type == "WARRANT":
                    class_names = ["WarrantStrategy"]
                elif asset_type == "CBBC":
                    class_names = ["CBBCStrategy"]
                elif asset_type == "IPO":
                    class_names = ["IPOStrategy"]
                else:
                    class_names = self._select_multi_strategies_by_regime(
                        strategy_fit, asset_type, anomaly_type, final_regime, regime_info,
                        market=mkt
                    )

                for class_name in class_names:
                    safe_sym = vt_symbol.replace(".", "_").replace(" ", "_")
                    strategy_name = f"{class_name}_{safe_sym}"

                    current_params = {
                        "diagnosis": {"features": extra, "score": cand.get("score", 50)},
                        "regime": {"regime": final_regime, "iv_percentile": iv_pct},
                        "anomaly_type": anomaly_type,
                        "asset_type": asset_type,
                    }
                    suggested = self.param_advisor.suggest(vt_symbol, class_name, current_params)
                    if suggested is None: suggested = {}
                    params = suggested

                    base = vt_symbol.split('.')[0] if '.' in vt_symbol else vt_symbol
                    dedup_key = (base, class_name)
                    if dedup_key in self._market_seen[mkt]:
                        logger.warning(f"[Gen] ⏭️ {mkt} 去重: {base}/{class_name} 已存在，跳过 {strategy_name}")
                        with write_lock: skipped += 1
                        continue

                    inserted = self._write_to_db(strategy_name, class_name, vt_symbol, mkt, params)
                    if inserted:
                        self._market_seen[mkt].add(dedup_key)
                        logger.info(f"[Gen] ✅ {strategy_name} → {class_name}({vt_symbol}) | {asset_type}|{anomaly_type}|{final_regime}|fit={strategy_fit}|market={mkt}")
                        with write_lock: written += 1
                    else:
                        with write_lock: skipped += 1
            except Exception as e:
                logger.error(f"[Gen] ❌ 生成失败 {cand.get('vt_symbol','?')}: {e}")

        with ThreadPoolExecutor(max_workers=self.gen_workers) as executor:
            futures = [executor.submit(_process_one, c) for c in candidates]
            for f in as_completed(futures):
                f.result()

        logger.info(f"[Gen] 共生成 {written} 个策略（跳过 {skipped} 个已存在/重复）")
        return written

    def generate_from_selector(self, selector_result: List[Dict],
                               regime_map: Dict[str, dict] = None,
                               market: str = "US") -> int:
        return self.generate(selector_result, regime_map, market)

    def _select_multi_strategies_by_regime(self, strategy_fit, asset_type, anomaly_type, regime, regime_info, market="US"):
        selected = set()
        has_equity = "equity" in strategy_fit
        has_option = "option" in strategy_fit
        has_hft = "hft" in strategy_fit
        micro_state = regime_info.get("micro_state", "normal")

        if has_equity:
            if anomaly_type != "none":
                primary = self._route(asset_type, anomaly_type, regime)
                if primary: selected.add(primary)
                else: selected.add(self.FALLBACK_STRATEGIES.get(asset_type, "GridStrategy"))
            else:
                probs = regime_info.get("probs", {})
                trend_prob = probs.get("trend", 0)
                range_prob = probs.get("range", 0)
                volatile_prob = probs.get("volatile", 0)

                if trend_prob == 0 and range_prob == 0 and volatile_prob == 0:
                    regime_to_strategy = {
                        "strong_bull": ["TrendStrategy"],
                        "bull": ["TrendStrategy", "GridStrategy"],
                        "range": ["GridStrategy"],
                        "volatile": ["DualThrustStrategy"],
                        "weak_bear": ["MomentumStrategy"],
                        "bear": ["DualThrustStrategy"],
                    }
                    for s in regime_to_strategy.get(regime, ["GridStrategy"]):
                        selected.add(s)
                else:
                    scores = {}
                    scores["TrendStrategy"] = trend_prob * 0.7 + (1 if regime in ("strong_bull","bull") else 0) * 0.3
                    scores["GridStrategy"] = range_prob * 0.8 + (0.5 if regime == "range" else 0) * 0.2
                    scores["DualThrustStrategy"] = volatile_prob * 0.9 + (0.5 if regime == "volatile" else 0) * 0.1
                    scores["MomentumStrategy"] = (1 - trend_prob - range_prob - volatile_prob) * 0.5
                    sorted_strategies = sorted(scores.items(), key=lambda x: -x[1])
                    for name, _ in sorted_strategies[:2]:
                        if scores[name] > 0.1:
                            selected.add(name)

        if has_option:
            if market == "US":
                if regime in ("strong_bull", "bull"):
                    selected.add("SellPutStrategy")
                elif regime in ("range", "volatile"):
                    selected.add("IronCondorStrategy")
                elif regime in ("weak_bear", "bear"):
                    selected.add("SellCallStrategy")
                else:
                    selected.add("IronCondorStrategy")
            else:
                logger.debug(f"[Gen] 港股 {market} 跳过期权策略")

        if has_hft:
            vol_level = regime_info.get("vol_level", "mid")
            if micro_state == "toxic_orderflow" and vol_level in ("low", "mid"):
                selected.add("ScalpingStrategy")
            elif micro_state == "liquidity_surge":
                selected.add("OrderFlowStrategy")
            elif micro_state == "tight_spread" or vol_level == "low":
                selected.add("VWAPStrategy")
            else:
                selected.add("VWAPStrategy")

        if market == "HK":
            selected -= self.HK_DISALLOWED_OPTION_STRATEGIES

        if not selected:
            fallback = self.FALLBACK_STRATEGIES.get(asset_type, "GridStrategy")
            selected.add(fallback)

        return list(selected)

    @staticmethod
    def _normalize_regime(regime: str) -> str:
        if not regime: return "range"
        normalized = StrategyGenerator.REGIME_NORMALIZATION.get(regime)
        if normalized is None:
            logger.warning(f"[Gen] 未知 regime '{regime}'，默认映射为 range")
            return "range"
        return normalized

    def _adjust_regime_by_diagnosis(self, base_regime: str, diagnosis_text: str) -> Optional[str]:
        if not diagnosis_text: return None
        for pattern, r in self.DIAGNOSIS_REGIME_RULES:
            if re.search(pattern, diagnosis_text, re.IGNORECASE):
                logger.debug(f"[Gen] 诊断文本匹配 → regime={r}")
                return r
        return None

    def _route(self, asset_type: str, anomaly: str, regime: str) -> Optional[str]:
        key = (asset_type, anomaly, regime)
        if key in self.ROUTE_TABLE: return self.ROUTE_TABLE[key]
        key2 = (asset_type, "*", regime)
        if key2 in self.ROUTE_TABLE: return self.ROUTE_TABLE[key2]
        key3 = (asset_type, anomaly, "*")
        if key3 in self.ROUTE_TABLE: return self.ROUTE_TABLE[key3]
        key4 = (asset_type, "*", "*")
        if key4 in self.ROUTE_TABLE: return self.ROUTE_TABLE[key4]
        return None

    def _write_to_db(self, strategy_name: str, class_name: str,
                     vt_symbol: str, market: str, params: dict) -> bool:
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT UNIQUE,
                    class_name TEXT,
                    vt_symbol TEXT,
                    market TEXT,
                    params TEXT,
                    status TEXT DEFAULT 'RUNNING',
                    active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            cur.execute("SELECT strategy_name FROM strategy_config WHERE strategy_name = ?", (strategy_name,))
            if cur.fetchone() is not None:
                logger.debug(f"[Gen] ⏭️ 跳过已存在策略 {strategy_name}")
                conn.close()
                return False

            base = vt_symbol.split('.')[0] if '.' in vt_symbol else vt_symbol
            cur.execute("""
                SELECT strategy_name FROM strategy_config
                WHERE market = ? AND class_name = ? AND vt_symbol LIKE ?
            """, (market, class_name, f"{base}.%"))
            existing = cur.fetchone()
            if existing is not None:
                logger.warning(f"[Gen] ⏭️ {market} 同底层 {base}+{class_name} 已存在 ({existing[0]})，跳过 {strategy_name}")
                conn.close()
                return False

            cur.execute("""
                INSERT INTO strategy_config
                (strategy_name, class_name, vt_symbol, market, params, status, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'RUNNING', 1, ?, ?)
            """, (strategy_name, class_name, vt_symbol, market,
                  json.dumps(params, ensure_ascii=False), now, now))
            conn.commit()
            conn.close()
            logger.info(f"[Gen] ✅ 新建策略 {strategy_name}")
            return True
        except Exception as e:
            logger.error(f"[DB] 写入失败 {strategy_name}: {e}")
            return False