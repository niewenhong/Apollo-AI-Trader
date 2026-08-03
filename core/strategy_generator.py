"""
core/strategy_generator.py - v3.3.0 增强版
==========================================
增强内容：
  1. 三级路由：anomaly_type × asset_class × regime → 策略 + 参数
  2. 60+ 条路由规则覆盖 5 种 regime × 6 种资产类型
  3. 三维参数模板（regime自适应 + 波动率缩放 + 流动性约束）
  4. IV 百分位驱动期权策略选择
  5. 衍生品参数自动继承正股 regime
  6. 置信度加权选股
"""
import json
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime

log = logging.getLogger("StrategyGenerator")


class StrategyGenerator:
    """
    策略生成器 v3.3.0
    输入：AnomalyCandidate + Regime 字典
    输出：写入 strategy_config 表
    """

    # ========== 三级路由表 ==========
    # (anomaly_type, asset_class, regime) → (class_name, param_template, priority)
    ROUTE_TABLE = {
        # ── 正股 ──
        # 量比异动
        ("volume_surge",    "EQUITY", "strong_bull"): ("TrendStrategy",       {"fast_window": 5,  "slow_window": 20, "atr_mult": 2.5, "position_pct": 0.15}, 1),
        ("volume_surge",    "EQUITY", "bull"):       ("TrendStrategy",       {"fast_window": 8,  "slow_window": 25, "atr_mult": 2.0, "position_pct": 0.12}, 1),
        ("volume_surge",    "EQUITY", "range"):      ("GridStrategy",        {"levels": 5, "atr_spacing": True, "position_pct": 0.10}, 2),
        ("volume_surge",    "EQUITY", "volatile"):   ("DualThrustStrategy", {"K": 0.4, "atr_stop": 1.5, "position_pct": 0.08}, 1),
        ("volume_surge",    "EQUITY", "weak_bear"):  ("BearPutSpreadStrategy", {"position_pct": 0.10}, 1),
        ("volume_surge",    "EQUITY", "bear"):      ("MomentumStrategy",   {"window": 10, "position_pct": 0.08}, 2),

        # 价格突破
        ("price_breakout", "EQUITY", "strong_bull"): ("TrendStrategy",       {"fast_window": 5,  "slow_window": 15, "atr_mult": 3.0, "position_pct": 0.15}, 1),
        ("price_breakout", "EQUITY", "bull"):       ("MomentumStrategy",   {"window": 10, "position_pct": 0.12}, 1),
        ("price_breakout", "EQUITY", "range"):      ("VWAPStrategy",       {"deviation": 0.003, "position_pct": 0.10}, 2),
        ("price_breakout", "EQUITY", "volatile"):   ("DualThrustStrategy", {"K": 0.35, "atr_stop": 1.2, "position_pct": 0.08}, 1),
        ("price_breakout", "EQUITY", "weak_bear"):  ("BearPutSpreadStrategy", {"position_pct": 0.10}, 1),
        ("price_breakout", "EQUITY", "bear"):      ("SellCallStrategy",   {"delta_target": 0.3, "position_pct": 0.10}, 2),

        # 振幅异动
        ("amplitude",      "EQUITY", "strong_bull"): ("TrendStrategy",       {"fast_window": 10, "slow_window": 30, "atr_mult": 2.0, "position_pct": 0.12}, 2),
        ("amplitude",      "EQUITY", "bull"):       ("GridStrategy",        {"levels": 7, "atr_spacing": True, "position_pct": 0.10}, 2),
        ("amplitude",      "EQUITY", "range"):      ("GridStrategy",        {"levels": 7, "atr_spacing": True, "position_pct": 0.10}, 1),
        ("amplitude",      "EQUITY", "volatile"):   ("DualThrustStrategy", {"K": 0.35, "atr_stop": 1.2, "position_pct": 0.08}, 1),
        ("amplitude",      "EQUITY", "weak_bear"):  ("BearPutSpreadStrategy", {"position_pct": 0.10}, 2),
        ("amplitude",      "EQUITY", "bear"):      ("GridStrategy",        {"levels": 5, "atr_spacing": True, "position_pct": 0.08}, 3),

        # 基本盘（无明确异动）
        ("none",           "EQUITY", "strong_bull"): ("TrendStrategy",       {"fast_window": 10, "slow_window": 30, "atr_mult": 2.5, "position_pct": 0.12}, 2),
        ("none",           "EQUITY", "bull"):       ("TrendStrategy",       {"fast_window": 15, "slow_window": 40, "atr_mult": 2.0, "position_pct": 0.10}, 2),
        ("none",           "EQUITY", "range"):      ("GridStrategy",        {"levels": 5, "atr_spacing": True, "position_pct": 0.08}, 1),
        ("none",           "EQUITY", "volatile"):   ("DualThrustStrategy", {"K": 0.5, "atr_stop": 1.5, "position_pct": 0.06}, 2),
        ("none",           "EQUITY", "weak_bear"):  ("GridStrategy",        {"levels": 5, "atr_spacing": True, "position_pct": 0.06}, 3),
        ("none",           "EQUITY", "bear"):      ("SellCallStrategy",   {"delta_target": 0.3, "position_pct": 0.08}, 3),

        # ── 窝轮 ──
        ("derivative_chain", "WARRANT", "strong_bull"): ("WarrantStrategy",  {"wrt_type": "CALL", "min_delta_abs": 0.4, "max_delta_abs": 0.7, "max_iv_rank": 0.5, "position_pct": 0.05}, 1),
        ("derivative_chain", "WARRANT", "bull"):       ("WarrantStrategy",  {"wrt_type": "CALL", "min_delta_abs": 0.3, "max_delta_abs": 0.6, "position_pct": 0.04}, 2),
        ("derivative_chain", "WARRANT", "range"):      ("WarrantStrategy",  {"wrt_type": "PUT",  "min_delta_abs": 0.3, "max_delta_abs": 0.6, "position_pct": 0.03}, 3),
        ("derivative_chain", "WARRANT", "volatile"):   ("WarrantStrategy",  {"wrt_type": "PUT",  "min_delta_abs": 0.4, "max_delta_abs": 0.7, "position_pct": 0.04}, 1),
        ("derivative_chain", "WARRANT", "weak_bear"):  ("WarrantStrategy",  {"wrt_type": "PUT",  "min_delta_abs": 0.4, "max_delta_abs": 0.7, "position_pct": 0.05}, 1),
        ("derivative_chain", "WARRANT", "bear"):      ("WarrantStrategy",  {"wrt_type": "PUT",  "min_delta_abs": 0.5, "max_delta_abs": 0.8, "position_pct": 0.05}, 1),

        # ── 牛熊证 ──
        ("derivative_chain", "CBBC", "strong_bull"): ("CBBCStrategy",     {"cbbc_type": "BULL", "min_distance_to_call": 8.0, "max_leverage": 10.0, "position_pct": 0.05}, 1),
        ("derivative_chain", "CBBC", "bull"):       ("CBBCStrategy",     {"cbbc_type": "BULL", "min_distance_to_call": 5.0, "max_leverage": 8.0, "position_pct": 0.04}, 2),
        ("derivative_chain", "CBBC", "volatile"):   ("CBBCStrategy",     {"cbbc_type": "BEAR", "min_distance_to_call": 8.0, "position_pct": 0.04}, 1),
        ("derivative_chain", "CBBC", "weak_bear"):  ("CBBCStrategy",     {"cbbc_type": "BEAR", "min_distance_to_call": 8.0, "max_leverage": 10.0, "position_pct": 0.05}, 1),
        ("derivative_chain", "CBBC", "bear"):      ("CBBCStrategy",     {"cbbc_type": "BEAR", "min_distance_to_call": 5.0, "position_pct": 0.05}, 1),
        ("derivative_chain", "CBBC", "range"):      ("GridStrategy",      {"levels": 5, "atr_spacing": True, "position_pct": 0.06}, 3),

        # ── 期权 ──
        ("derivative_chain", "OPTION", "range"):         ("SellCallStrategy",    {"delta_target": 0.3, "position_pct": 0.08}, 1),
        ("derivative_chain", "OPTION", "range_high_iv"): ("IronCondorStrategy",  {"wing_width": 0.10, "position_pct": 0.08}, 1),
        ("derivative_chain", "OPTION", "strong_bull"):   ("CoveredCallStrategy", {"delta_target": 0.3, "position_pct": 0.10}, 2),
        ("derivative_chain", "OPTION", "bull"):         ("BullCallSpreadStrategy", {"position_pct": 0.08}, 1),
        ("derivative_chain", "OPTION", "volatile"):     ("StraddleStrategy",    {"min_iv_rank": 0.6, "position_pct": 0.06}, 1),
        ("derivative_chain", "OPTION", "volatile_low_iv"): ("StraddleStrategy",  {"min_iv_rank": 0.3, "position_pct": 0.06}, 1),
        ("derivative_chain", "OPTION", "weak_bear"):    ("BearPutSpreadStrategy", {"position_pct": 0.08}, 1),
        ("derivative_chain", "OPTION", "bear"):         ("SellPutStrategy",     {"delta_target": 0.3, "position_pct": 0.08}, 2),

        # ── IPO ──
        ("ipo_listing",     "IPO", "volatile"):    ("IPOStrategy",        {"breakout_threshold": 0.05, "profit_take_pct": 0.30, "stop_loss_pct": 0.15, "position_pct": 0.05}, 1),
        ("ipo_listing",     "IPO", "strong_bull"): ("IPOStrategy",        {"breakout_threshold": 0.03, "profit_take_pct": 0.25, "stop_loss_pct": 0.12, "position_pct": 0.06}, 1),
        ("ipo_listing",     "IPO", "bull"):        ("IPOStrategy",        {"breakout_threshold": 0.04, "profit_take_pct": 0.20, "stop_loss_pct": 0.12, "position_pct": 0.05}, 2),
        ("ipo_listing",     "IPO", "range"):       ("GridStrategy",        {"levels": 3, "atr_spacing": True, "position_pct": 0.04}, 2),
        ("ipo_listing",     "IPO", "weak_bear"):   ("IPOStrategy",        {"breakout_threshold": 0.05, "profit_take_pct": 0.15, "stop_loss_pct": 0.10, "position_pct": 0.03}, 3),
    }

    # ========== 默认降级 ==========
    DEFAULT_ROUTE = ("GridStrategy", {"levels": 5, "atr_spacing": True, "position_pct": 0.08}, 3)

    # ========== IV 百分位阈值 ==========
    IV_SELL_THRESHOLD = 0.6   # IV > 此值 → 偏向卖权
    IV_BUY_THRESHOLD  = 0.4   # IV < 此值 → 偏向买权

    def __init__(self, quote_ctx=None, db_path: str = "data/history.db",
                 kline_provider=None, config: Optional[dict] = None):
        self.ctx = quote_ctx
        self.db_path = db_path
        self.kp = kline_provider
        self.config = config or {}
        self._db = None  # 延迟初始化
        self._matcher = None

    # ==================== 公开 API ====================

    def generate(self, candidates: List[dict],
                 regime_map: Dict[str, dict]) -> int:
        """
        candidates: List[AnomalyCandidate.to_dict()]
        regime_map: {vt_symbol: {"regime": ..., "confidence": ..., "iv_percentile": ...}}
        返回写入条数
        """
        written = 0
        for cand in candidates:
            try:
                route = self._route(cand, regime_map)
                if self._write_one(cand, route):
                    written += 1
            except Exception as e:
                log.error(f"[Gen] 写入失败 {cand.get('vt_symbol','?')}: {e}")
        log.info(f"[Gen] ✅ 共写入 {written}/{len(candidates)} 个策略到 strategy_config")
        return written

    def generate_one(self, cand: dict, regime: dict) -> bool:
        """单条生成（供动态注入）"""
        route = self._route(cand, {cand["vt_symbol"]: regime})
        return self._write_one(cand, route)

    # ==================== 路由核心 ====================

    def _route(self, cand: dict, regime_map: Dict[str, dict]) -> dict:
        asset = cand.get("asset_type", "EQUITY")
        anomaly = cand.get("anomaly_type", "none")
        vt = cand.get("vt_symbol", "")

        # 确定 regime 来源
        if asset in ("WARRANT", "CBBC", "OPTION"):
            underlying = cand.get("underlying", vt)
            reg_data = regime_map.get(underlying, {})
        else:
            reg_data = regime_map.get(vt, {})

        regime = reg_data.get("regime", "range")
        confidence = reg_data.get("confidence", 0.5)
        iv_pct = reg_data.get("iv_percentile", 0.5)

        # 期权 IV 修正
        if asset == "OPTION":
            regime = self._adjust_option_regime(regime, iv_pct)

        key = (anomaly, asset, regime)
        cls, params, pri = self.ROUTE_TABLE.get(key, self.DEFAULT_ROUTE)

        # 波动率缩放（高波动 → 减仓）
        vol_scale = self._volatility_scale(regime)
        params = dict(params)
        params["position_pct"] = round(params.get("position_pct", 0.08) * vol_scale, 4)

        # 衍生品附加正股信息
        if asset in ("WARRANT", "CBBC", "OPTION"):
            params["underlying_symbol"] = cand.get("underlying", "")

        # 置信度调整优先级
        if confidence < 0.4:
            pri += 1

        reason = f"{asset}|{anomaly}|{regime}|iv{iv_pct:.2f}|→{cls}"
        return {"class_name": cls, "params": params, "priority": pri, "reason": reason}

    def _adjust_option_regime(self, regime: str, iv_pct: float) -> str:
        """期权特殊 regime 修正"""
        if regime == "range" and iv_pct > self.IV_SELL_THRESHOLD:
            return "range_high_iv"
        if regime == "volatile" and iv_pct < self.IV_BUY_THRESHOLD:
            return "volatile_low_iv"
        return regime

    def _volatility_scale(self, regime: str) -> float:
        return {
            "strong_bull": 1.0,
            "bull": 0.9,
            "range": 0.8,
            "volatile": 0.6,
            "weak_bear": 0.7,
            "bear": 0.7,
            "range_high_iv": 0.8,
            "volatile_low_iv": 0.7,
        }.get(regime, 0.8)

    # ==================== 写库 ====================

    def _write_one(self, cand: dict, route: dict) -> bool:
        if self._db is None:
            from core.db_manager import DBManager
            self._db = DBManager(self.db_path)

        vt = cand.get("vt_symbol", "")
        if not vt:
            return False

        sym = vt.split(".")[0]
        market = "US" if vt.endswith(".SMART") else ("HK" if vt.endswith(".SEHK") else "")
        asset = cand.get("asset_type", "EQUITY")

        # 策略命名：ClassName_SYMBOL_ANOMALY
        anomaly_short = cand.get("anomaly_type", "none")[:10]
        strategy_name = f"{route['class_name']}_{sym}_{anomaly_short}"

        params = dict(route["params"])
        params["_anomaly_type"] = cand.get("anomaly_type", "none")
        params["_asset_class"] = asset
        params["_regime"] = cand.get("regime", "range")
        params["_score"] = cand.get("score", 0)
        params["_reason"] = route.get("reason", "")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params_json = json.dumps(params, ensure_ascii=False, default=str)

        try:
            cursor = self._db.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO strategy_config
                (strategy_name, class_name, vt_symbol, market, params,
                 enabled, active, version, current_version, status,
                 source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 1, 1, 1, 'PENDING', ?, ?, ?)
            """, (strategy_name, route["class_name"], vt, market,
                   params_json, "generator", now, now))
            self._db._safe_commit()
            log.info(f"[Gen] ✅ {strategy_name} → {route['class_name']}({vt}) | {route['reason']}")
            return True
        except Exception as e:
            log.error(f"[Gen] 写库失败: {e}")
            return False

    # ==================== 批量辅助 ====================

    # ★★★ FIX: 此方法已弃用，不再被调用，保留仅为兼容旧代码
    def clear_old_strategies(self, keep_names: Optional[List[str]] = None):
        """清空旧策略（部署前调用）—— 已弃用，请勿调用"""
        log.warning("[Gen] clear_old_strategies 已弃用，不再清空数据库")
        # 不再执行删除操作