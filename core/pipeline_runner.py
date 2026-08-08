# -*- coding: utf-8 -*-
"""
core/pipeline_runner.py - Apollo Trader v3.8.4
Pipeline 流程编排器：选股→诊股→regime→策略生成→部署
"""
import logging
from typing import List, Optional
from sqlalchemy import text

from core.strategy_generator import StrategyGenerator
from core.regime_predictor import AdaptiveRegimePredictor

log = logging.getLogger("PipelineRunner")


class PipelineRunner:
    """Pipeline 流程编排器"""

    def __init__(self, strategy_engine=None, db=None, config=None,
                 kp=None, regime_predictor: AdaptiveRegimePredictor = None,
                 quote_ctx_us=None, quote_ctx_hk=None):
        self.se = strategy_engine
        self.db = db
        self.config = config or {}
        self.kp = kp
        self.regime_predictor = regime_predictor
        self.quote_us = quote_ctx_us
        self.quote_hk = quote_ctx_hk

    # ==================== 公开接口 ====================

    def run(self, market: str = None, symbol: str = None, candidates: list = None):
        """
        运行完整 pipeline
        入口：
          - market: "US"/"HK" 自动选股
          - symbol: 单个股票代码（如 "AAPL"）
          - candidates: 预定义候选列表（用于手动持仓接管）
        """
        log.info(f"[PipelineRunner] run (market={market}, symbol={symbol}, "
                 f"candidates={len(candidates) if candidates else 0})")

        # 确定候选列表
        if candidates is None:
            if symbol:
                vt_symbol = self._build_vt_symbol(symbol, market or "US")
                candidates = [{
                    "vt_symbol": vt_symbol,
                    "market": market or "US",
                    "asset_type": "EQUITY",
                    "anomaly_type": "none",
                    "source": "manual_symbol",
                    "score": 95,
                    "extra": {},
                    "strategy_fit": ["equity"],
                }]
            elif market:
                candidates = self._auto_select(market)
            else:
                log.error("[PipelineRunner] 必须提供 market、symbol 或 candidates")
                return

        if not candidates:
            log.info("[PipelineRunner] 无候选标的")
            return

        # 诊股
        self._diagnose(candidates)
        # Regime 预测
        regime_map = self._predict_regime(candidates)
        # 策略生成
        self._generate(candidates, regime_map)
        # 部署
        self._deploy(candidates[0].get("market", "US"))

        log.info("[PipelineRunner] ✅ 完成")

    def takeover_manual_positions(self):
        """检测未托管持仓 → 调用 run(candidates=...)"""
        log.info("[PipelineRunner] 开始检测未托管持仓...")
        if not self.se:
            log.warning("[PipelineRunner] strategy_engine 未注入，跳过")
            return

        # 获取未托管持仓候选列表（通过 strategy_engine 的内部网关）
        candidates = self._fetch_unmanaged_candidates()
        if not candidates:
            log.info("[PipelineRunner] 无未托管持仓")
            return

        log.info(f"[PipelineRunner] 未托管持仓: {len(candidates)} 只")
        self.run(candidates=candidates)

    # ==================== 内部辅助方法 ====================

    def _build_vt_symbol(self, symbol: str, market: str) -> str:
        if "." in symbol:
            return symbol
        suffix = ".SMART" if market == "US" else ".SEHK"
        return f"{symbol}{suffix}"

    def _auto_select(self, market: str) -> list:
        """执行自动选股"""
        from ai.stock_selector import StockSelector
        ctx = self.quote_us if market == "US" else self.quote_hk
        selector = StockSelector(quote_ctx=ctx, db=self.db, kline_provider=self.kp, config=self.config)
        selected = selector.run(markets=[market])
        log.info(f"[PipelineRunner] 自动选股完成: {len(selected)} 只")
        return selected

    def _diagnose(self, candidates: list):
        """诊股：US/HK 各自并行，每市场最多4个worker"""
        from ai.stock_diagnosis import StockDiagnosis
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # 按市场分组
        groups = {"US": [], "HK": []}
        for item in candidates:
            market = item.get("market")
            if market in groups:
                groups[market].append(item)

        for market, items in groups.items():
            if not items:
                continue
            ctx = self.quote_us if market == "US" else self.quote_hk
            if not ctx:
                continue

            # 每个市场独立线程池，最大4个worker
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_item = {}
                for item in items:
                    vt = item.get("vt_symbol", "")
                    if not vt:
                        continue
                    # 每个任务创建独立的诊断实例（避免quote_ctx线程安全问题）
                    diag = StockDiagnosis(
                        quote_ctx=ctx,
                        db=self.db,
                        kline_provider=self.kp
                    )
                    future = executor.submit(diag.diagnose, vt)
                    future_to_item[future] = (vt, item)

                for future in as_completed(future_to_item):
                    vt, item = future_to_item[future]
                    try:
                        result = future.result()
                        summary = result.get("summary", "")
                        item.setdefault("extra", {})["diagnosis"] = summary
                        log.info(f"[PipelineRunner] 诊股 {vt}: {summary}")
                    except Exception as e:
                        log.warning(f"[PipelineRunner] 诊股 {vt} 失败: {e}")

    def _predict_regime(self, candidates: list) -> dict:
        """Regime 预测"""
        regime_map = {}
        if self.regime_predictor:
            for item in candidates:
                vt = item.get("vt_symbol", "")
                if not vt:
                    continue
                market = item.get("market", "US")
                result = self.regime_predictor.predict(vt, market=market)
                regime_map[vt] = {
                    "regime": result.get("regime", "range_mid"),
                    "confidence": result.get("confidence", 0.5),
                    "iv_percentile": result.get("iv_percentile", 0.5),
                }
                log.info(f"[PipelineRunner] Regime {vt}: {regime_map[vt]}")
        else:
            for item in candidates:
                vt = item.get("vt_symbol", "")
                if vt:
                    regime_map[vt] = {"regime": "range_mid", "confidence": 0.5, "iv_percentile": 0.5}
        return regime_map

    def _generate(self, candidates: list, regime_map: dict):
        """策略生成（使用 DBManager 的实际数据库路径）"""
        # ★ 关键修复：使用 DBManager 实际的数据库路径
        if self.db and hasattr(self.db, 'db_url'):
            raw_url = self.db.db_url
            if raw_url.startswith("sqlite:///"):
                db_path = raw_url[len("sqlite:///"):]
            else:
                db_path = "data/apollo.db"
        else:
            db_path = "data/apollo.db"

        first_market = candidates[0].get("market", "US")
        ctx = self.quote_us if first_market == "US" else self.quote_hk
        generator = StrategyGenerator(
            quote_ctx=ctx,
            db_path=db_path,
            kline_provider=self.kp,
            regime_predictor=self.regime_predictor,
            db=self.db,
            config=self.config,
        )
        written = generator.generate(candidates, regime_map)
        log.info(f"[PipelineRunner] 策略生成完成: {written} 个")

        # 强制提交并刷新，确保后续 boot 能读到
        if self.db and hasattr(self.db, 'session'):
            session = self.db.session
            session.commit()
            if hasattr(self.db, 'engine'):
                try:
                    self.db.engine.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                except Exception:
                    pass

    def _deploy(self, market: str):
        """部署策略（部署前刷新缓存）"""
        if not self.se:
            log.warning("[PipelineRunner] strategy_engine 未注入，跳过部署")
            return

        # 强制刷新 DB 会话缓存
        if self.db and hasattr(self.db, 'session'):
            self.db.session.expire_all()

        result = self.se.boot(operator=f"pipeline_{market}", market=market)
        deployed = result.get("deployed", []) if isinstance(result, dict) else []
        log.info(f"[PipelineRunner] 部署完成: {len(deployed)} 个策略")

    # ==================== 持仓获取（接管用） ====================

    def _fetch_unmanaged_candidates(self) -> list:
        """
        从所有网关获取实际持仓，去重后返回未托管候选列表
        格式：[{"vt_symbol":..., "market":..., "asset_type":"EQUITY", ...}]
        """
        positions = self._fetch_all_positions()
        if not positions:
            return []

        managed = self._get_managed_symbols()
        us_candidates, hk_candidates = self._group_unmanaged(positions, managed)
        return us_candidates + hk_candidates

    def _fetch_all_positions(self) -> list:
        """从所有 gateway 获取实际持仓"""
        all_positions = []
        for label, me in [("US", self.se.main_us if self.se else None),
                          ("HK", self.se.main_hk if self.se else None)]:
            if me is None:
                continue
            gw = me.gateways.get(f"FUTU_{label}")
            if gw is None:
                continue
            try:
                positions = gw.get_positions()
                if positions:
                    for pos in positions:
                        all_positions.append({
                            'symbol': pos.get('symbol', ''),
                            'exchange': str(pos.get('exchange', label)),
                            'qty': float(pos.get('qty', 0)),
                            'cost_price': float(pos.get('cost_price', 0)),
                            'pnl': float(pos.get('pnl', 0)),
                        })
            except Exception as e:
                log.warning(f"[PipelineRunner] 获取 {label} 持仓失败: {e}")
        return all_positions

    def _get_managed_symbols(self) -> set:
        """获取当前所有已部署策略管理的 vt_symbol 集合"""
        managed = set()
        if self.se:
            for name, info in getattr(self.se, '_deployed', {}).items():
                vt = info.get("vt_symbol", "")
                if vt:
                    managed.add(vt)
            for label, me in [("US", self.se.main_us), ("HK", self.se.main_hk)]:
                if me is None:
                    continue
                cta = me.get_engine("CtaStrategy") if me else None
                if cta is None:
                    continue
                for sname, sobj in getattr(cta, 'strategies', {}).items():
                    vt = getattr(sobj, 'vt_symbol', '') or ''
                    if vt:
                        managed.add(vt)
        return managed

    def _group_unmanaged(self, positions: list, managed: set) -> tuple:
        """按市场分组，构建 candidates 列表"""
        us_candidates = []
        hk_candidates = []
        for pos in positions:
            symbol = pos['symbol']
            exchange = pos['exchange']
            qty = pos['qty']
            if not symbol or qty <= 0:
                continue
            if 'SMART' in exchange or exchange == 'US':
                vt_symbol = f"{symbol}.SMART"
                market = "US"
            elif 'SEHK' in exchange or exchange == 'HK':
                vt_symbol = f"{symbol}.SEHK"
                market = "HK"
            else:
                vt_symbol = f"{symbol}.SMART"
                market = "US"
            if vt_symbol in managed:
                continue
            candidate = {
                "vt_symbol": vt_symbol,
                "market": market,
                "asset_type": "EQUITY",
                "anomaly_type": "none",
                "source": "manual_position",
                "score": 85,
                "extra": {
                    "qty": qty,
                    "cost_price": pos.get('cost_price', 0),
                    "pnl": pos.get('pnl', 0),
                },
                "strategy_fit": ["equity"],
            }
            if market == "US":
                candidate["strategy_fit"].extend(["option", "hft"])
                us_candidates.append(candidate)
            else:
                candidate["strategy_fit"].append("option")
                hk_candidates.append(candidate)
        return us_candidates, hk_candidates