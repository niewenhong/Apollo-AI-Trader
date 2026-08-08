# -*- coding: utf-8 -*-
"""
core/strategy_engine.py - Apollo Trader v3.8.4
基线：v3.8.2
v3.8.4 变更：
  - 修复 _deploy_to_cta 中 sub_manager.subscribe 调用
    （SubscriptionManager v3.8.3+ 不再提供 subscribe 方法，改为直接通过 quote_ctx 订阅）
  - 移除所有与手动持仓接管相关的代码（移至 pipeline_runner.py）
"""
import importlib
import json
import time
import threading
import logging
import traceback
import hashlib
import re
import copy
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Exchange
from vnpy_ctastrategy.engine import CtaEngine

from core.strategy_generator import StrategyGenerator
from core.db_manager import DBManager
from core.prelive_gate import PreliveGate
from ai.param_advisor import ParamAdvisor
from core.regime_predictor import AdaptiveRegimePredictor

logger = logging.getLogger("StrategyEngine")


class StrategyEngine:

    CLASS_NAME_ALIAS = {
        "OrderFlowStrategy": "TickOrderFlowStrategy",
    }

    CLASS_MODULE_MAP = {
        "MultiIndicatorStrategy": "strategies.equity.multi_indicator_strategy",
        "DualThrustStrategy": "strategies.equity.dual_thrust_strategy",
        "GridStrategy": "strategies.equity.grid_strategy",
        "TrendStrategy": "strategies.equity.trend_strategy",
        "VWAPStrategy": "strategies.equity.vwap_strategy",
        "OrderFlowStrategy": "strategies.equity.order_flow_strategy",
        "TickOrderFlowStrategy": "strategies.equity.order_flow_strategy",
        "ScalpingStrategy": "strategies.equity.scalping_strategy",
        "MomentumStrategy": "strategies.futures.momentum_strategy",
        "SellCallStrategy": "strategies.options.sell_call_strategy",
        "SellPutStrategy": "strategies.options.sell_put_strategy",
        "CashSecuredPutStrategy": "strategies.options.cash_secured_put_strategy",
        "CoveredCallStrategy": "strategies.options.covered_call_strategy",
        "BullCallSpreadStrategy": "strategies.options.bull_call_spread_strategy",
        "BearPutSpreadStrategy": "strategies.options.bear_put_spread_strategy",
        "IronCondorStrategy": "strategies.options.iron_condor_strategy",
        "StraddleStrategy": "strategies.options.straddle_strategy",
        "WarrantStrategy": "strategies.structured_products.warrant_strategy",
        "CBBCStrategy": "strategies.structured_products.cbbc_strategy",
        "IPOStrategy": "strategies.ipo.ipo_strategy",
    }

    DERIVATIVE_KEYWORDS = ["CBBC", "Warrant", "BullBear"]

    INIT_WAIT_INTERVAL = 0.1
    INIT_WAIT_TIMEOUT = 15.0
    POST_INIT_DELAY = 0.2
    START_RETRY_COUNT = 2
    START_RETRY_DELAY = 0.5
    TRADING_WAIT_TIMEOUT = 3.0

    def __init__(self, main_us: MainEngine = None, main_hk: MainEngine = None,
                 main_engines: dict = None,
                 db: DBManager = None, config: dict = None,
                 quote_ctx=None, advisor=None,
                 regime_predictor: AdaptiveRegimePredictor = None,
                 risk_manager=None, order_manager=None,
                 account_manager=None, lifecycle_manager=None,
                 user_manager=None):
        if main_engines:
            self.main_us = main_engines.get("US", main_us)
            self.main_hk = main_engines.get("HK", main_hk)
        else:
            self.main_us = main_us
            self.main_hk = main_hk
        self.main_engine = self.main_us
        self.db = db
        self.config = config or {}
        self.quote_ctx = quote_ctx
        self.telegram_bot = None
        self.regime_predictor = regime_predictor

        self.risk = risk_manager
        self.order_mgr = order_manager
        self.account = account_manager
        self.lifecycle = lifecycle_manager
        self.user_mgr = user_manager

        self.sub_manager = None
        self.kline_provider = None
        self.matcher = None

        gate_cfg = self.config.get("prelive_gate", {})
        self.prelive_gate = PreliveGate(db, thresholds=gate_cfg.get("thresholds"))
        self.advisor = advisor if advisor is not None else ParamAdvisor(db)
        self.hot_reload_interval = gate_cfg.get("hot_reload_interval", 600)
        self.backtest_days = gate_cfg.get("backtest_days", 60)
        self.backtest_interval = gate_cfg.get("backtest_interval", "1m")
        self.deploy_workers = gate_cfg.get("deploy_workers", 8)

        self.strategies = {}
        self._deployed = {}
        self._deploy_lock = threading.Lock()

        self._hot_reload_stop = threading.Event()
        self._init_cta_engines()

        self._contract_ready_flags = {"US": False, "HK": False}
        if self.main_us:
            self.main_us.event_engine.register("eContractReady", self._make_on_ready("US"))
        if self.main_hk:
            self.main_hk.event_engine.register("eContractReady", self._make_on_ready("HK"))
        logger.info("[StrategyEngine] 等待合约就绪事件...")

        if self.lifecycle:
            self.lifecycle.set_strategy_engine(self)

        logger.info("[StrategyEngine] ✅ 初始化完成 (v3.8.4)")

    # ==================== 初始化 ====================

    def _init_cta_engines(self):
        for label, me in [("US", self.main_us), ("HK", self.main_hk)]:
            if me is None:
                continue
            try:
                cta = me.get_engine("CtaStrategy")
                if cta is None:
                    cta = me.add_app_from_module("vnpy_ctastrategy")
                    cta = me.get_engine("CtaStrategy")
                if cta is None:
                    logger.error(f"[StrategyEngine] ❌ {label} CTA 引擎获取失败")
                    continue
                cta.init_engine()
                logger.info(f"[StrategyEngine] ✅ {label} CTA 引擎初始化完成")
                registered = []
                for class_name, module_path in self.CLASS_MODULE_MAP.items():
                    try:
                        mod = importlib.import_module(module_path)
                        cls = getattr(mod, class_name, None)
                        if cls is None:
                            alias = self.CLASS_NAME_ALIAS.get(class_name)
                            if alias:
                                cls = getattr(mod, alias, None)
                        if cls is not None:
                            cta.classes[class_name] = cls
                            registered.append(class_name)
                        else:
                            logger.warning(f"[StrategyEngine] {label} 未找到类 {class_name}")
                    except Exception as e:
                        logger.error(f"[StrategyEngine] {label} 注册 {class_name} 失败: {e}")
                logger.info(f"[StrategyEngine] {label} 已注册策略类: {registered}")
                self._purge_cta_residual_strategies(label, cta)
            except Exception as e:
                logger.error(f"[StrategyEngine] {label} CTA 初始化异常: {e}")

    def _purge_cta_residual_strategies(self, market: str, cta):
        if cta is None:
            return
        existing = list(getattr(cta, 'strategies', {}).keys())
        if not existing:
            return
        logger.info(f"[StrategyEngine] {market} CTA 中发现 {len(existing)} 个残留策略，正在清理...")
        for name in existing:
            try:
                if name in cta.strategies:
                    try:
                        cta.stop_strategy(name)
                    except Exception:
                        pass
                    try:
                        cta.remove_strategy(name)
                        logger.info(f"[StrategyEngine] ✅ 已移除 CTA 残留策略: {name}")
                    except Exception as e:
                        logger.warning(f"[StrategyEngine] 移除残留策略 {name} 失败: {e}")
            except Exception as e:
                logger.error(f"[StrategyEngine] 清理残留策略 {name} 异常: {e}")

    def _make_on_ready(self, market: str):
        def handler(event):
            if self._contract_ready_flags.get(market):
                return
            self._contract_ready_flags[market] = True
            logger.info(f"[StrategyEngine] {market} 合约就绪")
            if all(self._contract_ready_flags.values()):
                logger.info("[StrategyEngine] 双市场合约就绪，开始部署策略")
                self._load_strategies_from_db()
        return handler

    # ==================== 启动流程 ====================

    def _load_strategies_from_db(self):
        db_configs = self.db.get_all_strategies(enabled_only=True)
        if not db_configs:
            logger.warning("[StrategyEngine] strategy_config 表为空，触发流水线补全...")
            self._trigger_pipeline()
            db_configs = self.db.get_all_strategies(enabled_only=True)
            if not db_configs:
                logger.error("[StrategyEngine] ❌ 流水线执行后 strategy_config 仍为空")
                return
        logger.info(f"[StrategyEngine] 从数据库加载 {len(db_configs)} 个策略")
        for cfg in db_configs:
            if not cfg.get("vt_symbol"):
                symbol, market = self._parse_strategy_name(cfg["strategy_name"])
                if symbol and market:
                    self.db.save_strategy(cfg["strategy_name"], cfg["class_name"],
                                          symbol, market, cfg.get("params", {}),
                                          source="auto_fix", modifier="system:fix_empty_vt_symbol")
                    cfg["vt_symbol"] = symbol
                    cfg["market"] = market
            cfg["vt_symbol"] = self._normalize_vt_symbol(cfg.get("vt_symbol", ""), cfg.get("market", "US"))
        self.boot(configs=db_configs, operator="system:bootstrap")

    def _trigger_pipeline(self):
        try:
            from ai.stock_selector import StockSelector
            from concurrent.futures import ThreadPoolExecutor, as_completed
            selector = StockSelector(
                quote_ctx_us=self.quote_ctx or self._get_quote_ctx(),
                quote_ctx_hk=None, db=self.db, config=self.config,
            )
            selected = selector.select_all(markets=["US", "HK"])
            logger.info(f"[Pipeline] ✅ 选股完成: {len(selected)} 只")
            if not selected:
                logger.error("[Pipeline] ❌ 选股返回空")
                return

            regime_map = {}
            if self.regime_predictor:
                symbols = [c["vt_symbol"] for c in selected if c.get("vt_symbol")]
                with ThreadPoolExecutor(max_workers=5) as ex:
                    futs = {ex.submit(self.regime_predictor.predict, s): s for s in symbols}
                    for f in as_completed(futs):
                        s = futs[f]
                        try:
                            result = f.result()
                            regime_map[s] = {
                                "regime": result.get("regime", "range_mid"),
                                "confidence": result.get("confidence", 0.5),
                                "iv_percentile": result.get("iv_percentile", 0.5),
                            }
                        except Exception:
                            regime_map[s] = {"regime": "range_mid", "confidence": 0.5, "iv_percentile": 0.5}
            else:
                for item in selected:
                    vt = item.get("vt_symbol", "")
                    if vt:
                        regime_map[vt] = {"regime": "range_mid", "confidence": 0.5, "iv_percentile": 0.5}

            db_path = getattr(self.db, 'db_path', '') or 'data/history.db'
            generator = StrategyGenerator(
                quote_ctx=self.quote_ctx or self._get_quote_ctx(),
                db_path=db_path, kline_provider=getattr(self, 'kline_provider', None),
                regime_predictor=self.regime_predictor, db=self.db, config=self.config,
            )
            written = generator.generate(selected, regime_map)
            logger.info(f"[Pipeline] ✅ 策略生成完成: {written} 个")
        except Exception as e:
            logger.error(f"[Pipeline] ❌ 流水线触发失败: {e}\n{traceback.format_exc()}")

    def _get_quote_ctx(self):
        try:
            gw = self.main_us.gateways.get("FUTU_US")
            if gw and hasattr(gw, 'quote_ctx'):
                return gw.quote_ctx
        except Exception:
            pass
        return None

    # ==================== 部署核心 ====================

    def _deploy_to_cta(self, strategy_name: str, class_name: str,
                       vt_symbol: str, params: dict, market: str) -> bool:
        with self._deploy_lock:
            params = copy.deepcopy(params)
            cta = self._get_cta_engine(market)
            if cta is None:
                logger.error(f"[StrategyEngine] {market} CTA 引擎不可用")
                return False
            vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
            if not vt_symbol:
                logger.error(f"[StrategyEngine] {strategy_name} vt_symbol 为空")
                return False

            current_hash = self._compute_params_hash(params)
            if strategy_name in self._deployed:
                if self._deployed[strategy_name]["params_hash"] == current_hash:
                    logger.debug(f"[StrategyEngine] {strategy_name} 参数无变化，跳过部署")
                    return True
                else:
                    logger.info(f"[StrategyEngine] {strategy_name} 参数变化，重新部署")
                    try:
                        cta.stop_strategy(strategy_name)
                    except Exception:
                        pass
                    try:
                        cta.remove_strategy(strategy_name)
                    except Exception:
                        pass
                    time.sleep(0.3)

            logger.info(f"[StrategyEngine] 部署 {strategy_name} → {vt_symbol} ({class_name}) [{market}]")
            try:
                if class_name not in cta.classes:
                    logger.error(f"[StrategyEngine] '{class_name}' 未注册")
                    return False
                self._set_status(strategy_name, "INITING", f"正在初始化 {class_name}@{vt_symbol}")
                cta.add_strategy(class_name=class_name, strategy_name=strategy_name,
                                 vt_symbol=vt_symbol, setting=params)
                init_success = cta.init_strategy(strategy_name)
                if not init_success:
                    self._set_status(strategy_name, "FAILED", "init_strategy 返回失败")
                    self._safe_remove(cta, strategy_name)
                    return False
                strategy_obj = cta.strategies.get(strategy_name)
                if strategy_obj is None:
                    self._set_status(strategy_name, "FAILED", "策略对象不在 cta.strategies 中")
                    return False
                elapsed = 0.0
                while elapsed < self.INIT_WAIT_TIMEOUT:
                    if getattr(strategy_obj, 'inited', False):
                        break
                    time.sleep(self.INIT_WAIT_INTERVAL)
                    elapsed += self.INIT_WAIT_INTERVAL
                else:
                    self._set_status(strategy_name, "FAILED", f"初始化超时 ({self.INIT_WAIT_TIMEOUT}s)")
                    self._safe_remove(cta, strategy_name)
                    return False
                self._set_status(strategy_name, "INITED", "初始化完成，准备启动")
                time.sleep(self.POST_INIT_DELAY)

                self._set_status(strategy_name, "STARTING", "正在启动策略")
                start_success = False
                last_error = ""
                for attempt in range(self.START_RETRY_COUNT):
                    try:
                        cta.start_strategy(strategy_name)
                        t_elapsed = 0.0
                        while t_elapsed < self.TRADING_WAIT_TIMEOUT:
                            if getattr(strategy_obj, 'trading', False):
                                start_success = True
                                break
                            time.sleep(0.1)
                            t_elapsed += 0.1
                        if start_success:
                            break
                        else:
                            last_error = f"trading 未在 {self.TRADING_WAIT_TIMEOUT}s 内变为 True"
                    except Exception as e:
                        last_error = str(e)
                    if attempt < self.START_RETRY_COUNT - 1:
                        time.sleep(self.START_RETRY_DELAY)
                        strategy_obj = cta.strategies.get(strategy_name)
                        if strategy_obj is None or not getattr(strategy_obj, 'inited', False):
                            try:
                                cta.init_strategy(strategy_name)
                                for _ in range(50):
                                    s = cta.strategies.get(strategy_name)
                                    if s and getattr(s, 'inited', False):
                                        strategy_obj = s
                                        break
                                    time.sleep(0.1)
                            except Exception:
                                pass
                if not start_success:
                    self._set_status(strategy_name, "FAILED", f"启动失败: {last_error}")
                    self._safe_remove(cta, strategy_name)
                    return False
                self.strategies[strategy_name] = strategy_obj
                self._set_status(strategy_name, "RUNNING", "正常运行中")
                logger.info(f"[StrategyEngine] ✅ {strategy_name} add→init→start 完成 [{market}]")

                # 设置策略对象的引用
                if self.lifecycle:
                    strategy_obj.lifecycle_manager = self.lifecycle
                user_id = params.get('user_id', 'SYSTEM')
                strategy_obj.user_id = user_id

                # v3.8.4: 行情订阅通过 quote_ctx 直接完成（cta_engine.init_strategy 已自动订阅）
                # SubscriptionManager v3.8.3+ 不再提供 subscribe 方法，此处不调用

                # 显式传递完整参数给 LifecycleManager → RiskManager
                if self.lifecycle:
                    self.lifecycle.on_strategy_deployed(
                        strategy_name,
                        vt_symbol=vt_symbol,
                        class_name=class_name,
                        market=market
                    )

                return True
            except Exception as e:
                err = f"部署异常: {e}"
                self._set_status(strategy_name, "FAILED", err)
                logger.error(f"[StrategyEngine] {strategy_name} {err}\n{traceback.format_exc()}")
                try:
                    self._safe_remove(cta, strategy_name)
                except Exception:
                    pass
                return False

    def _set_status(self, name: str, status: str, msg: str = ""):
        self.db.set_strategy_status(name, status, msg)
        if name in self._deployed:
            self._deployed[name]["status"] = status
            self._deployed[name]["status_msg"] = msg

    def _safe_remove(self, cta, strategy_name: str):
        try:
            if strategy_name in cta.strategies:
                try:
                    cta.stop_strategy(strategy_name)
                except Exception:
                    pass
                try:
                    cta.remove_strategy(strategy_name)
                except Exception:
                    pass
        except Exception:
            pass

    def _get_cta_engine(self, market: str = "US") -> Optional[CtaEngine]:
        target_me = self.main_hk if market == "HK" else self.main_us
        if target_me is None:
            return None
        try:
            return target_me.get_engine("CtaStrategy")
        except Exception as e:
            logger.error(f"[StrategyEngine] 获取 {market} CTA 引擎失败: {e}")
            return None

    # ==================== boot（多线程部署 + market过滤 + 去重）====================

    def boot(self, configs: list = None, operator: str = "system",
             market: str = None) -> dict:
        """
        v3.8.2: 支持 market 参数按市场过滤；同市场内同底层+同类去重
        """
        if configs is None:
            configs = self.db.get_all_strategies(enabled_only=True)

        if market:
            configs = [c for c in configs if c.get("market", "US") == market]
            logger.info(f"[Boot] ═══ {market} 策略启动（{self.deploy_workers}线程并行）═══ 共 {len(configs)} 个")
        else:
            logger.info(f"[Boot] ═══ 全市场策略启动（{self.deploy_workers}线程并行）═══ 共 {len(configs)} 个")

        if not configs:
            logger.error("[Boot] ❌ 无可用策略配置")
            return {"all_pass": False, "deployed": [], "failed": [], "skipped": 0}

        # 同市场内去重：同一底层symbol + 同一class_name 只保留第一个
        seen = set()
        deduped = []
        for cfg in configs:
            vt = cfg.get("vt_symbol", "")
            cn = cfg.get("class_name", "")
            base = vt.split('.')[0] if '.' in vt else vt
            key = (base, cn)
            if key in seen:
                logger.warning(f"[Boot] ⏭️ 去重跳过: {cfg['strategy_name']} ({base}/{cn} 已存在)")
                continue
            seen.add(key)
            deduped.append(cfg)
        configs = deduped

        deployed = []
        failed = []
        skipped = 0

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=self.deploy_workers) as executor:
            future_map = {}
            for cfg in configs:
                name = cfg["strategy_name"]
                current_hash = self._compute_params_hash(cfg.get("params", {}))
                if name in self._deployed and self._deployed[name]["params_hash"] == current_hash:
                    logger.debug(f"[Boot] {name} 已部署且参数未变，跳过")
                    skipped += 1
                    deployed.append(name)
                    continue
                future = executor.submit(self._validate_and_deploy, cfg, operator)
                future_map[future] = name

            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    result = future.result()
                    if result.get("deployed"):
                        deployed.append(name)
                        cfg = next((c for c in configs if c["strategy_name"] == name), {})
                        self._deployed[name] = {
                            "params_hash": self._compute_params_hash(cfg.get("params", {})),
                            "updated_at": cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            "status": "RUNNING",
                            "status_msg": "正常运行中",
                            "vt_symbol": cfg.get("vt_symbol", ""),
                            "class_name": cfg.get("class_name", ""),
                            "market": cfg.get("market", "US"),
                        }
                    else:
                        failed.append(name)
                except Exception as e:
                    logger.error(f"[Boot] {name} 部署异常: {e}")
                    failed.append(name)

        summary = {"all_pass": len(failed) == 0, "deployed": deployed, "failed": failed, "skipped": skipped}
        if skipped:
            logger.info(f"[Boot] 跳过 {skipped} 个已部署且参数未变的策略")
        if summary["all_pass"]:
            logger.info(f"[Boot] ✅ 全部 {len(deployed)} 个策略部署成功")
        else:
            logger.warning(f"[Boot] ⚠️ 成功 {len(deployed)}, 失败 {len(failed)}: {failed}")
        return summary

    def _validate_and_deploy(self, cfg: dict, operator: str = "system") -> dict:
        name = cfg["strategy_name"]
        vt_symbol = cfg.get("vt_symbol", "")
        class_name = cfg["class_name"]
        params = cfg.get("params", {}).copy()
        market = cfg.get("market", "US")
        version = cfg.get("current_version", 1) or cfg.get("version", 1)

        # 同市场内同底层+同类去重检查
        base = vt_symbol.split('.')[0] if '.' in vt_symbol else vt_symbol
        for deployed_name, info in list(self._deployed.items()):
            if (info.get("market") == market and
                info.get("vt_symbol", "").split('.')[0] == base and
                info.get("class_name") == class_name):
                logger.warning(f"[Deploy] ⏭️ {market} {base}/{class_name} 已部署 ({deployed_name})，跳过 {name}")
                return {"deployed": False, "reason": "duplicate_same_market_base_class"}

        if not vt_symbol:
            symbol, m = self._parse_strategy_name(name)
            if symbol and m:
                self.db.save_strategy(name, class_name, symbol, m, params,
                                      source="auto_fix", modifier="system:fix_empty_vt_symbol")
                vt_symbol = symbol
                market = m
                cfg["vt_symbol"] = symbol
                cfg["market"] = m
        vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
        if not vt_symbol:
            error_msg = "vt_symbol 为空"
            self.db.log_deploy(name, version, "deploy", operator, "failed", error_msg)
            return {"deployed": False, "reason": error_msg}
        try:
            strategy_cls, actual_class_name = self._resolve_class(class_name)
            if strategy_cls is None:
                error_msg = f"无法解析类: {class_name}"
                self.db.log_deploy(name, version, "deploy", operator, "failed", error_msg)
                return {"deployed": False, "reason": error_msg}
            suggested = self.advisor.suggest(vt_symbol, actual_class_name, params)
            if suggested:
                params.update(suggested)
            if self._deploy_to_cta(name, actual_class_name, vt_symbol, params, market):
                self.db.mark_deployed(name, version, operator)
                self.db.log_deploy(name, version, "deploy", operator, "success", "gate_disabled")
                logger.info(f"[StrategyEngine] ✅ {name} 全链路部署成功 (v{version}) [{market}]")
                return {"deployed": True, "version": version, "gate": {"pass": True}}
            error_msg = "CTA引擎部署失败"
            self.db.log_deploy(name, version, "deploy", operator, "failed", error_msg)
            return {"deployed": False, "reason": error_msg}
        except Exception as e:
            logger.error(f"[StrategyEngine] {name} 部署异常: {e}\n{traceback.format_exc()}")
            self.db.log_deploy(name, version, "deploy", operator, "failed", str(e))
            return {"deployed": False, "reason": f"异常: {str(e)}"}

    # ==================== 热加载 ====================

    def check_and_reload_changed(self, operator: str = "system") -> dict:
        db_configs = self.db.get_all_strategies(enabled_only=True)
        db_map = {cfg["strategy_name"]: cfg for cfg in db_configs}
        db_names = set(db_map.keys())
        running_names = set(self._deployed.keys())

        added, updated, removed = [], [], []

        to_process = []
        for name in db_names:
            if name not in self._deployed:
                to_process.append((name, "add"))
            else:
                cfg = db_map[name]
                new_hash = self._compute_params_hash(cfg.get("params", {}))
                if self._deployed[name]["params_hash"] != new_hash:
                    to_process.append((name, "update"))

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=self.deploy_workers) as executor:
            future_map = {}
            for name, action in to_process:
                future = executor.submit(self._handle_one_change, name, action, db_map[name], operator)
                future_map[future] = (name, action)
            for future in as_completed(future_map):
                name, action = future_map[future]
                try:
                    result = future.result()
                    if result:
                        if action == "add":
                            added.append(name)
                        else:
                            updated.append(name)
                except Exception as e:
                    logger.error(f"[HotReload] {name} 处理异常: {e}")

        for name in running_names - db_names:
            if self._remove_strategy(name, operator=operator, reason="DB中已删除"):
                removed.append(name)

        summary = {"added": added, "updated": updated, "removed": removed}
        if any(summary.values()):
            logger.info(f"[HotReload] +{len(added)} ~{len(updated)} -{len(removed)}")
        return summary

    def _handle_one_change(self, name: str, action: str, cfg: dict, operator: str) -> bool:
        if action == "update":
            old_params = self._deployed[name].get("params", {})
            new_params = cfg.get("params", {})
            if self.db:
                try:
                    self.db.archive_strategy_params(
                        strategy_name=name, vt_symbol=cfg.get("vt_symbol", ""),
                        class_name=cfg.get("class_name", ""),
                        old_params=old_params, new_params=new_params,
                        changed_by=operator, reason="参数变化-自动更新")
                except Exception as e:
                    logger.warning(f"[HotReload] 归档参数失败 {name}: {e}")
            self._stop_and_remove_cta(name)

        logger.info(f"[HotReload] {'新增' if action == 'add' else '更新'} {name}")
        result = self._validate_and_deploy(cfg, operator=operator)
        if result.get("deployed"):
            self._deployed[name] = {
                "params_hash": self._compute_params_hash(cfg.get("params", {})),
                "updated_at": cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "status": "RUNNING", "status_msg": "热加载部署",
                "params": cfg.get("params", {}),
                "vt_symbol": cfg.get("vt_symbol", ""),
                "class_name": cfg.get("class_name", ""),
                "market": cfg.get("market", "US"),
            }
            return True
        return False

    def _stop_and_remove_cta(self, strategy_name: str):
        for label, me in [("US", self.main_us), ("HK", self.main_hk)]:
            if me is None:
                continue
            cta = self._get_cta_engine(label)
            if cta and strategy_name in cta.strategies:
                try:
                    cta.stop_strategy(strategy_name)
                    cta.remove_strategy(strategy_name)
                    logger.info(f"[StrategyEngine] ✅ {strategy_name} 已从 {label} CTA 移除")
                except Exception as e:
                    logger.error(f"[StrategyEngine] 移除 {strategy_name} 失败: {e}")

    # ==================== 热加载后台线程 ====================

    def start_hot_reload(self, interval: int = None):
        if interval:
            self.hot_reload_interval = interval
        self._hot_reload_stop.clear()
        def _loop():
            logger.info(f"[StrategyEngine] 🔄 热加载已启动 (间隔 {self.hot_reload_interval}s)")
            while not self._hot_reload_stop.is_set():
                try:
                    changed = self.check_and_reload_changed(operator="hot_reload")
                    if changed:
                        logger.info(f"[StrategyEngine] 热加载处理: {changed}")
                except Exception as e:
                    logger.error(f"[StrategyEngine] 热加载异常: {e}")
                self._hot_reload_stop.wait(self.hot_reload_interval)
        t = threading.Thread(target=_loop, daemon=True, name="HotReload")
        t.start()
        return t

    def stop_hot_reload(self):
        self._hot_reload_stop.set()
        logger.info("[StrategyEngine] 热加载已停止")

    # ==================== 公开接口 ====================

    def get_status(self) -> dict:
        return {
            "count": len(self.strategies),
            "deployed_count": len(self._deployed),
            "running_count": sum(1 for info in self._deployed.values() if info.get("status") == "RUNNING"),
            "last_heart_beat": datetime.now().isoformat(),
            "strategies": {"count": len(self.strategies), "names": list(self.strategies.keys())},
        }

    def get_all_strategies(self) -> List[Any]:
        return list(self.strategies.values())

    def get_running_strategies_info(self) -> List[dict]:
        info_list = []
        for name, data in self._deployed.items():
            cfg = self.db.get_strategy(name) or {}
            info_list.append({
                "strategy_name": name,
                "class_name": cfg.get("class_name", ""),
                "vt_symbol": cfg.get("vt_symbol", ""),
                "market": cfg.get("market", "US"),
                "params": data.get("params", {}),
            })
        return info_list

    def start_all(self):
        for name in list(self._deployed.keys()):
            try:
                market = self._deployed[name].get("market", "US")
                cta = self._get_cta_engine(market)
                if cta and name in cta.strategies:
                    cta.start_strategy(name)
            except Exception:
                pass

    def stop_all(self):
        for name in list(self._deployed.keys()):
            try:
                market = self._deployed[name].get("market", "US")
                cta = self._get_cta_engine(market)
                if cta and name in cta.strategies:
                    cta.stop_strategy(name)
            except Exception:
                pass

    # ==================== Telegram 状态格式化 ====================
    def format_status(self) -> str:
        """
        生成策略状态文本，限制长度不超过 3800 字符
        """
        strategies = getattr(self, 'strategies', {})
        if not strategies:
            return "📊 <b>策略状态</b>\n  无运行中策略"

        lines = ["📊 <b>策略状态</b>"]
        us_count = hk_count = 0
        running_count = 0
        details = []
        for name, s in strategies.items():
            vt = getattr(s, 'vt_symbol', '?')
            tr = getattr(s, 'trading', False)
            p = getattr(s, 'pos', 0)
            icon = "🟢" if tr else "🔴"
            market = "US" if "US" in str(vt) or ".SMART" in str(vt) else "HK"
            if market == "US":
                us_count += 1
            else:
                hk_count += 1
            if tr:
                running_count += 1
            details.append(f"  {icon} {name} ({vt}) pos={p}")

        # 概要
        lines.append(f"  🇺🇸 美股: {us_count}  🇭🇰 港股: {hk_count}  总计: {len(strategies)}  运行中: {running_count}")
        lines.append("─" * 30)

        # 详细列表（如果太多则只显示前 20 条 + 省略提示）
        max_lines = 120  # 预留空间给概要部分
        if len(details) > max_lines:
            lines.extend(details[:max_lines])
            lines.append(f"  ... 还有 {len(details) - max_lines} 个策略未显示，使用 /details 查看全部")
        else:
            lines.extend(details)

        result = "\n".join(lines)
        # 确保不超过 3900 字符（留余量）
        if len(result) > 3850:
            result = result[:3850] + "\n...(消息过长，已截断)"
        return result

    def add_strategy(self, strategy_name: str, class_name: str, vt_symbol: str,
                     params: dict, market: str = "US"):
        cfg = {"strategy_name": strategy_name, "class_name": class_name,
               "vt_symbol": vt_symbol, "params": params, "market": market}
        return self._validate_and_deploy(cfg, operator="manual")

    def remove_strategy(self, strategy_name: str, operator: str = "manual", reason: str = ""):
        self._stop_and_remove_cta(strategy_name)
        self._deployed.pop(strategy_name, None)
        self.strategies.pop(strategy_name, None)
        if self.db:
            try:
                self.db.set_strategy_status(strategy_name, "REMOVED", reason or "手动移除")
            except Exception:
                pass
        logger.info(f"[StrategyEngine] 🗑️ {strategy_name} 已移除")

    # ==================== 工具方法 ====================
    def _compute_params_hash(self, params: dict) -> str:
        s = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(s.encode('utf-8')).hexdigest()

    def _resolve_class(self, class_name: str):
        if class_name in self.CLASS_MODULE_MAP:
            module_path = self.CLASS_MODULE_MAP[class_name]
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name, None)
                if cls is None:
                    alias = self.CLASS_NAME_ALIAS.get(class_name)
                    if alias:
                        cls = getattr(mod, alias, None)
                return cls, class_name
            except Exception as e:
                logger.error(f"[StrategyEngine] 解析类 {class_name} 失败: {e}")
                return None, class_name
        return None, class_name

    def _normalize_vt_symbol(self, vt_symbol: str, market: str = "US") -> str:
        if not vt_symbol:
            return ""
        vt_symbol = vt_symbol.strip()
        if "." in vt_symbol:
            return vt_symbol
        if market == "US":
            return f"{vt_symbol}.SMART"
        elif market == "HK":
            return f"{vt_symbol}.SEHK"
        return vt_symbol

    def _parse_strategy_name(self, strategy_name: str):
        try:
            parts = strategy_name.rsplit('_', 1)
            if len(parts) == 2:
                sym_part = parts[1]
                if "." in sym_part:
                    prefix = sym_part.split('.')[0]
                    if sym_part.endswith(".SMART"):
                        return prefix, "US"
                    if sym_part.endswith(".SEHK"):
                        return prefix, "HK"
                return sym_part, "US"
        except Exception:
            pass
        return "", "US"

    def _to_futu_symbol(self, vt_symbol: str, market: str) -> str:
        if not vt_symbol:
            return ""
        if vt_symbol.endswith(".SMART"):
            return vt_symbol.replace(".SMART", "")
        if vt_symbol.endswith(".SEHK"):
            return "HK." + vt_symbol.replace(".SEHK", "")
        return vt_symbol
