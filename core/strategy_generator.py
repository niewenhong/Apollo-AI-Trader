"""
core/strategy_generator.py - 策略生成器 v2.8.6
功能：选股 → 诊股 → regime → 匹配合适策略 → 写入 strategy_config 表
修正：
  1. _db_adapter 直接委托给 DBManager（不再重复建表逻辑）
  2. save_diagnosis 参数自适应（与 db_manager 保持一致）
  3. regime_trainer 导入路径修正为 core.regime_trainer
  4. 删除 ON CONFLICT 中的语法错误（原文件 CONFLICT 拼写错误）
  5. regime 计算失败时降级为 debug 级别
"""
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional

import sqlite3

log = logging.getLogger("StrategyGenerator")

from core.kline_provider import KlineProvider

# 各策略的默认参数模板
DEFAULT_PARAMS = {
    "TrendStrategy": {
        "trend_window": 24,
        "entry_atr": 1.5,
        "fixed_size": 100,
        "stop_loss_atr": 2.0,
    },
    "GridStrategy": {
        "grid_step": 0.5,
        "grid_levels": 5,
        "fixed_size": 50,
        "upper_bound": 0.05,
        "lower_bound": -0.05,
    },
    "OrderFlowStrategy": {
        "volume_threshold": 1.5,
        "price_tick": 0.01,
        "fixed_size": 50,
    },
    "MultiIndicatorStrategy": {
        "fast_ma": 10,
        "slow_ma": 30,
        "rsi_period": 14,
        "rsi_upper": 70,
        "rsi_lower": 30,
        "fixed_size": 100,
    },
    "DualThrustStrategy": {
        "k1": 0.5,
        "k2": 0.5,
        "fixed_size": 100,
    },
    "VWAPStrategy": {
        "vwap_window": 20,
        "deviation_pct": 0.002,
        "fixed_size": 100,
    },
}

# Regime → 优先策略映射（当 matcher 不可用时降级使用）
REGIME_FALLBACK = {
    "trend": "TrendStrategy",
    "range": "GridStrategy",
    "volatile": "OrderFlowStrategy",
}


class StrategyGenerator:
    """
    策略生成器：读取 ai_stock_pool → 对每只股票做诊股+regime → 匹配最优策略 → 写入 strategy_config 表
    """

    def __init__(self, quote_ctx, db_path: str = "data/history.db",
                 matcher=None, param_advisor=None,
                 kline_provider=None):
        self.ctx = quote_ctx
        self.db_path = db_path
        self.matcher = matcher
        self.advisor = param_advisor
        self.kp = kline_provider
        self._init_tables()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_tables(self):
        """确保 strategy_config 表存在（核心表）"""
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS strategy_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    class_name TEXT NOT NULL,
                    vt_symbol TEXT NOT NULL,
                    market TEXT DEFAULT 'US',
                    params_json TEXT DEFAULT '{}',
                    version INTEGER DEFAULT 1,
                    active INTEGER DEFAULT 1,
                    source TEXT DEFAULT 'pipeline',
                    modifier TEXT DEFAULT 'system',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
            log.info("[Gen] ✅ strategy_config 表就绪")
        finally:
            conn.close()

    def generate_for_pool(self, pool: List[Dict]) -> int:
        """
        对选股池中的每只股票：诊股 → regime → 匹配策略 → 写入数据库
        返回成功写入的策略数量
        """
        count = 0
        for item in pool:
            vt_symbol = item.get("stock_code") or item.get("vt_symbol")
            if not vt_symbol:
                continue

            try:
                diagnosis = self._diagnose(vt_symbol)
                regime_data = self._get_or_compute_regime(vt_symbol)
                strategy_class = self._match_strategy(vt_symbol, regime_data)
                params = self._build_params(vt_symbol, strategy_class)

                name = f"{strategy_class}_{vt_symbol.split('.')[0]}"
                market = "US" if "SMART" in vt_symbol else "HK"
                self._write_strategy(name, strategy_class, vt_symbol, market, params, active=True)
                count += 1
                log.info(f"[Gen] ✅ {name} → {strategy_class}({vt_symbol}) regime={regime_data.get('regime','?')}")

            except Exception as e:
                log.error(f"[Gen] {vt_symbol} 生成失败: {e}")

        log.info(f"[Gen] 🎉 共写入 {count} 个策略到 strategy_config 表")
        return count

    def generate_from_selector(self, selector_results: List[Dict]) -> int:
        """
        直接从选股结果生成策略
        """
        pool = []
        for s in selector_results:
            pool.append({
                "stock_code": s.get("vt_symbol", ""),
                "score": s.get("score", 0),
                "reason": s.get("reason", ""),
            })
        return self.generate_for_pool(pool)

    # ============ 内部方法 ============

    def _diagnose(self, vt_symbol: str) -> Dict:
        """调用 StockDiagnosis 做诊股（注入 KlineProvider）"""
        try:
            from ai.stock_diagnosis import StockDiagnosis
            diag = StockDiagnosis(
                quote_ctx=self.ctx,
                db=self._db_adapter(),
                kline_provider=self.kp,
            )
            result = diag.diagnose(vt_symbol)
            return result
        except Exception as e:
            log.warning(f"[Gen] {vt_symbol} 诊股跳过: {e}")
            return {}

    def _get_or_compute_regime(self, vt_symbol: str) -> Dict:
        """获取最新 regime（优先读库，没有则计算）"""
        try:
            from core.regime_trainer import RegimeTrainer
            trainer = RegimeTrainer(
                quote_ctx=self.ctx,
                db_path=self.db_path,
                kline_provider=self.kp,
            )
            existing = trainer.get_latest(vt_symbol)
            if existing:
                return existing
            futu_code = self._to_futu_code(vt_symbol)
            regime = trainer.compute(futu_code)
            if regime is None:
                regime = RegimeTrainer.default_regime()
            trainer.save(vt_symbol, regime)
            return regime
        except Exception as e:
            log.debug(f"[Gen] {vt_symbol} regime 计算失败，使用默认: {e}")
            return {"regime": "range", "prob_trend": 0.33, "prob_range": 0.34, "prob_volatile": 0.33}

    def _match_strategy(self, vt_symbol: str, regime_data: Dict) -> str:
        """根据 regime 匹配最优策略"""
        if self.matcher:
            try:
                symbol = vt_symbol.split(".")[0]
                market = "US" if "SMART" in vt_symbol else "HK"
                weights = self.matcher.get_weights(symbol, market)
                best = max(weights, key=weights.get)
                log.info(f"[Gen] {vt_symbol} matcher 权重: {weights} → 选 {best}")
                return best
            except Exception as e:
                log.warning(f"[Gen] matcher 失败，降级: {e}")

        regime = regime_data.get("regime", "range")
        return REGIME_FALLBACK.get(regime, "TrendStrategy")

    def _build_params(self, vt_symbol: str, strategy_class: str) -> Dict:
        """构建策略参数（默认 + advisor 优化）"""
        params = DEFAULT_PARAMS.get(strategy_class, {}).copy()

        if self.advisor:
            try:
                optimized = self.advisor.suggest(vt_symbol, strategy_class, params)
                if optimized:
                    params.update(optimized)
                    log.info(f"[Gen] {vt_symbol} 参数已优化: {params}")
            except Exception as e:
                log.warning(f"[Gen] advisor 失败: {e}")

        return params

    def _write_strategy(self, name: str, class_name: str, vt_symbol: str,
                         market: str, params: Dict, active: bool = True):
        """写入 strategy_config 表（UPSERT）"""
        conn = self._connect()
        try:
            cur = conn.execute("SELECT version FROM strategy_config WHERE name=?", (name,))
            row = cur.fetchone()
            version = (row[0] + 1) if row else 1

            conn.execute("""
                INSERT INTO strategy_config
                (name, class_name, vt_symbol, market, params_json, version, active, source, modifier, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pipeline', 'system', datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    class_name=excluded.class_name,
                    vt_symbol=excluded.vt_symbol,
                    market=excluded.market,
                    params_json=excluded.params_json,
                    version=excluded.version,
                    active=excluded.active,
                    source='pipeline',
                    modifier='system',
                    updated_at=datetime('now')
            """, (
                name, class_name, vt_symbol, market,
                json.dumps(params),
                version,
                1 if active else 0,
            ))
            conn.commit()
        finally:
            conn.close()

    def _to_futu_code(self, vt_symbol: str) -> str:
        """vt_symbol → futu code: AAPL.SMART → US.AAPL"""
        parts = vt_symbol.split(".")
        code = parts[0]
        if "SMART" in vt_symbol or "NASDAQ" in vt_symbol or "NYSE" in vt_symbol:
            return f"US.{code}"
        elif "SEHK" in vt_symbol:
            return f"HK.{code}"
        return code

    def _db_adapter(self):
        """
        给 StockDiagnosis 用的轻量 DB 适配器
        直接委托给 DBManager（单一数据源，避免重复建表逻辑）
        参数自适应：无论调用方怎么传参都能正确处理
        """
        from core.db_manager import DBManager
        real_db = DBManager(self.db_path)

        class _Adapter:
            def __init__(self, db: DBManager):
                self.db = db

            def save_diagnosis(self, symbol, score=50.0,
                               verdict="未知", details=None,
                               vt_symbol=""):
                # 参数自适应纠正
                if isinstance(score, dict):
                    details = score
                    score = 50.0
                if isinstance(verdict, dict):
                    details = verdict
                    verdict = "未知"
                if details is not None and not isinstance(details, dict):
                    try:
                        details = json.loads(details) if isinstance(details, str) else {}
                    except (json.JSONDecodeError, TypeError):
                        details = {}

                return self.db.save_diagnosis(
                    symbol=symbol,
                    score=score,
                    verdict=verdict,
                    details=details,
                    vt_symbol=vt_symbol or symbol
                )

        return _Adapter(real_db)

    # ============ 供 Scheduler / main.py 调用 ============

    def run_full_pipeline(self, selector_universe: Optional[List[str]] = None) -> Dict:
        """
        完整流水线：选股 → 诊股 → regime → 生成策略 → 写库
        """
        try:
            from ai.stock_selector import AIStockSelector
            tmp = AIStockSelector(
                quote_ctx=self.ctx, db=self._db_adapter(),
                top_n=10, market="US")
            if selector_universe is None:
                selector_universe = tmp._get_default_universe()
            if self.kp is not None:
                vt_list = [KlineProvider.futu_to_vt(c) for c in selector_universe]
                self.kp.preload(vt_list, ktype="K_DAY", days=120)
        except Exception as e:
            log.error(f"[Gen] universe 准备失败: {e}")
            return {"error": str(e), "strategies_written": 0}

        try:
            selector = AIStockSelector(
                quote_ctx=self.ctx,
                db=self._db_adapter(),
                top_n=10,
                market="US",
                kline_provider=self.kp,
            )
            selected = selector.select(universe=selector_universe)
            log.info(f"[Gen] 选股完成: {len(selected)} 只")
        except Exception as e:
            log.error(f"[Gen] 选股失败: {e}")
            return {"error": str(e), "strategies_written": 0}

        written = self.generate_from_selector(selected)

        if self.kp is not None:
            s = self.kp.stats()
            log.info(f"[Gen] KlineProvider 统计: {s}")

        return {
            "selected": len(selected),
            "strategies_written": written,
            "timestamp": datetime.now().isoformat(),
        }
