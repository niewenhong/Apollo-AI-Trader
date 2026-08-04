# -*- coding: utf-8 -*-
"""
strategy_generator.py - v3.3.2 增强版路由（基于诊断文本动态调整regime）
变更：
  v3.3.2 - generate() 不再调用 _clear_old_strategies，避免双市场互相删除
            _write_to_db() 保留已有 created_at，不刷新首次创建时间
"""
import json
import logging
import re
from typing import List, Dict, Optional, Any
from datetime import datetime

from ai.param_advisor import ParamAdvisor
from core.regime_trainer import RegimeTrainer

logger = logging.getLogger("StrategyGenerator")


class StrategyGenerator:
    """策略生成器：路由 + 参数建议 + 写DB"""

    ROUTE_TABLE = {
        # ---- EQUITY ----
        ("EQUITY", "none", "strong_bull"): "TrendStrategy",
        ("EQUITY", "none", "bull"): "TrendStrategy",
        ("EQUITY", "none", "range"): "GridStrategy",
        ("EQUITY", "none", "volatile"): "DualThrustStrategy",
        ("EQUITY", "none", "weak_bear"): "MomentumStrategy",
        ("EQUITY", "none", "bear"): "DualThrustStrategy",
        # EQUITY + anomaly → 优先异动策略
        ("EQUITY", "volume_surge", "*"): "TrendStrategy",
        ("EQUITY", "price_breakout", "*"): "DualThrustStrategy",
        ("EQUITY", "amplitude", "*"): "ScalpingStrategy",
        # ---- IPO ----
        ("IPO", "*", "*"): "IPOStrategy",
        # ---- WARRANT ----
        ("WARRANT", "*", "*"): "WarrantStrategy",
        # ---- CBBC ----
        ("CBBC", "*", "*"): "CBBCStrategy",
        # ---- OPTION ----
        ("OPTION", "*", "strong_bull"): "SellPutStrategy",
        ("OPTION", "*", "bull"): "SellPutStrategy",
        ("OPTION", "*", "range"): "IronCondorStrategy",
        ("OPTION", "*", "volatile"): "IronCondorStrategy",
        ("OPTION", "*", "weak_bear"): "SellCallStrategy",
        ("OPTION", "*", "bear"): "SellCallStrategy",
    }

    FALLBACK_STRATEGIES = {
        "EQUITY": "GridStrategy",
        "IPO": "IPOStrategy",
        "WARRANT": "WarrantStrategy",
        "CBBC": "CBBCStrategy",
        "OPTION": "IronCondorStrategy",
    }

    # 诊断文本 → regime 映射（关键词权重）
    DIAGNOSIS_REGIME_RULES = [
        (r"(多头排列|强势突破|52周高位.*9[5-9]%|52周高位.*100%)", "strong_bull"),
        (r"(站上MA20|周线偏多|均线多头|金叉)", "bull"),
        (r"(空头排列|跌破MA20|死叉|52周低位)", "weak_bear"),
        (r"(大幅震荡|高波动|振幅扩大)", "volatile"),
    ]

    def __init__(self, quote_ctx=None, db_path: str = "data/history.db",
                 kline_provider=None, regime_trainer: RegimeTrainer = None,
                 param_advisor: ParamAdvisor = None,
                 db=None, config: dict = None):
        self.ctx = quote_ctx
        self.db_path = db_path
        self.kp = kline_provider
        self.regime_trainer = regime_trainer
        self.param_advisor = param_advisor or ParamAdvisor(db)
        self.db = db
        self.config = config or {}
        logger.info("[StrategyGenerator] v3.3.2 增强路由（诊断驱动）初始化完成")

    def generate(self, candidates: List[Dict], regime_map: Dict[str, dict] = None,
                 market: str = "US") -> int:
        if not candidates:
            logger.warning("[Gen] 候选列表为空")
            return 0

        if regime_map is None:
            symbols = [c["vt_symbol"] for c in candidates]
            if self.regime_trainer:
                regime_map = self.regime_trainer.batch_compute(symbols)
            else:
                regime_map = {s: {"regime": "range", "confidence": 0.5, "iv_percentile": 0.5} for s in symbols}

        # v3.3.2: 不再调用 _clear_old_strategies(market)
        # 同名策略由 _write_to_db 的 INSERT OR REPLACE 自动覆盖
        # 不同名的新策略直接插入，旧策略由热加载对账清理

        written = 0
        for cand in candidates:
            try:
                vt_symbol = cand["vt_symbol"]
                asset_type = cand.get("asset_type", "EQUITY")
                anomaly_type = cand.get("anomaly_type", "none")
                market = cand.get("market", market)

                regime_info = regime_map.get(vt_symbol, {})
                base_regime = regime_info.get("regime", "range")
                iv_pct = regime_info.get("iv_percentile", 0.5)

                extra = cand.get("extra", {})
                diagnosis_text = extra.get("diagnosis", "")
                adjusted_regime = self._adjust_regime_by_diagnosis(base_regime, diagnosis_text)
                final_regime = adjusted_regime if adjusted_regime else base_regime

                class_name = self._route(asset_type, anomaly_type, final_regime)
                if not class_name:
                    class_name = self.FALLBACK_STRATEGIES.get(asset_type, "GridStrategy")

                safe_sym = vt_symbol.replace(".", "_").replace(" ", "_")
                strategy_name = f"{class_name}_{safe_sym}"

                current_params = {
                    "diagnosis": {"features": extra, "score": cand.get("score", 50)},
                    "regime": {"regime": final_regime, "iv_percentile": iv_pct},
                    "anomaly_type": anomaly_type,
                    "asset_type": asset_type,
                }
                suggested = self.param_advisor.suggest(vt_symbol, class_name, current_params)
                if suggested is None:
                    suggested = {}
                params = suggested

                self._write_to_db(strategy_name, class_name, vt_symbol, market, params)

                logger.info(
                    f"[Gen] ✅ {strategy_name} → {class_name}({vt_symbol}) | "
                    f"{asset_type}|{anomaly_type}|{final_regime}|iv{iv_pct:.2f}|→{class_name}"
                )
                written += 1
            except Exception as e:
                logger.error(f"[Gen] ❌ 生成失败 {cand.get('vt_symbol','?')}: {e}")

        logger.info(f"[Gen] 共生成 {written} 个策略")
        return written

    def _adjust_regime_by_diagnosis(self, base_regime: str, diagnosis_text: str) -> Optional[str]:
        if not diagnosis_text:
            return None
        for pattern, regime in self.DIAGNOSIS_REGIME_RULES:
            if re.search(pattern, diagnosis_text, re.IGNORECASE):
                logger.debug(f"[Gen] 诊断文本 '{diagnosis_text}' 匹配 '{pattern}' → regime={regime}")
                return regime
        return None

    def _route(self, asset_type: str, anomaly: str, regime: str) -> Optional[str]:
        key = (asset_type, anomaly, regime)
        if key in self.ROUTE_TABLE:
            return self.ROUTE_TABLE[key]
        key2 = (asset_type, "*", regime)
        if key2 in self.ROUTE_TABLE:
            return self.ROUTE_TABLE[key2]
        key3 = (asset_type, anomaly, "*")
        if key3 in self.ROUTE_TABLE:
            return self.ROUTE_TABLE[key3]
        key4 = (asset_type, "*", "*")
        if key4 in self.ROUTE_TABLE:
            return self.ROUTE_TABLE[key4]
        return None

    def _clear_old_strategies(self, market: str):
        """保留此方法供手动清理使用，generate() 不再调用"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM strategy_config WHERE market = ?", (market,))
            conn.commit()
            conn.close()
            logger.info(f"[Gen] 已清除 {market} 市场旧策略记录（手动调用）")
        except Exception as e:
            logger.warning(f"[Gen] 清除旧策略失败: {e}")

    def _write_to_db(self, strategy_name: str, class_name: str,
                     vt_symbol: str, market: str, params: dict):
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_config (
                    strategy_name TEXT PRIMARY KEY,
                    class_name TEXT,
                    vt_symbol TEXT,
                    market TEXT,
                    params TEXT,
                    status TEXT DEFAULT 'RUNNING',
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # v3.3.2: 保留已有记录的 created_at（首次创建时间不被刷新）
            cur.execute("SELECT created_at FROM strategy_config WHERE strategy_name = ?", (strategy_name,))
            row = cur.fetchone()
            created_at = row[0] if row and row[0] else now

            cur.execute("""
                INSERT OR REPLACE INTO strategy_config
                (strategy_name, class_name, vt_symbol, market, params, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
            """, (
                strategy_name, class_name, vt_symbol, market,
                json.dumps(params, ensure_ascii=False),
                created_at, now,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[DB] 写入失败 {strategy_name}: {e}")
