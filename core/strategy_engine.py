"""
core/strategy_engine.py - Apollo Trader v3.1.4 双引擎版（严格幂等 + 状态追踪 + 外部 Advisor）
"""
import importlib
import json
import time
import threading
import logging
import traceback
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Exchange
from vnpy_ctastrategy.engine import CtaEngine

from core.db_manager import DBManager
from core.prelive_gate import PreliveGate
from ai.param_advisor import ParamAdvisor

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
    }

    INIT_WAIT_INTERVAL = 0.1
    INIT_WAIT_TIMEOUT = 15.0
    POST_INIT_DELAY = 0.2
    START_RETRY_COUNT = 2
    START_RETRY_DELAY = 0.5
    TRADING_WAIT_TIMEOUT = 3.0

    def __init__(self, main_us: MainEngine, main_hk: MainEngine,
                 db: DBManager, config: dict = None,
                 quote_ctx=None, advisor=None):
        self.main_us = main_us
        self.main_hk = main_hk
        self.main_engine = main_us
        self.db = db
        self.config = config or {}
        self.quote_ctx = quote_ctx
        self.telegram_bot = None

        self.strategies = {}
        self._deployed = {}

        # ★ FIX: 新增 sub_manager 属性，由 main.py 注入
        self.sub_manager = None

        gate_cfg = self.config.get("prelive_gate", {})
        self.prelive_gate = PreliveGate(db, thresholds=gate_cfg.get("thresholds"))
        if advisor is not None:
            self.advisor = advisor
        else:
            self.advisor = ParamAdvisor(db)
        self.hot_reload_interval = gate_cfg.get("hot_reload_interval", 600)
        self.backtest_days = gate_cfg.get("backtest_days", 60)
        self.backtest_interval = gate_cfg.get("backtest_interval", "1m")

        self._hot_reload_stop = threading.Event()
        self._init_cta_engines()

        self._contract_ready_flags = {"US": False, "HK": False}
        self.main_us.event_engine.register("eContractReady", self._make_on_ready("US"))
        self.main_hk.event_engine.register("eContractReady", self._make_on_ready("HK"))
        logger.info("[StrategyEngine] 等待合约就绪事件...")

    def _init_cta_engines(self):
        for label, me in [("US", self.main_us), ("HK", self.main_hk)]:
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
            except Exception as e:
                logger.error(f"[StrategyEngine] {label} CTA 初始化异常: {e}")

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
            from ai.stock_selector import AIStockSelector
            selector = AIStockSelector(quote_ctx=self.quote_ctx or self._get_quote_ctx(), db=self.db, market="US")
            selected = selector.select()
            logger.info(f"[Pipeline] ✅ 选股完成: {len(selected)} 只")
            if not selected:
                logger.error("[Pipeline] ❌ 选股返回空")
                return
            from core.strategy_generator import StrategyGenerator
            from core.strategy_matcher import StrategyMatcher
            matcher = StrategyMatcher(db_path=getattr(self.db, 'db_path', ''))
            generator = StrategyGenerator(quote_ctx=self.quote_ctx or self._get_quote_ctx(),
                                          matcher=matcher, param_advisor=self.advisor)
            written = generator.generate_from_selector(selected)
            logger.info(f"[Pipeline] ✅ 策略生成完成: {written} 个写入 strategy_config")
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

    @staticmethod
    def _compute_params_hash(params: dict) -> str:
        return hashlib.md5(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()

    # ★ FIX: 新增辅助方法，将 vt_symbol 转为富途格式
    @staticmethod
    def _to_futu_symbol(vt_symbol: str, market: str) -> str:
        """如 AAPL.SMART → US.AAPL ; 00700.SEHK → HK.00700"""
        if not vt_symbol:
            return ""
        parts = vt_symbol.split(".")
        if len(parts) == 2:
            sym, exch = parts
            if exch.upper() in ("SMART", "NASDAQ", "NYSE"):
                return f"US.{sym}"
            elif exch.upper() in ("SEHK", "HKEX"):
                return f"HK.{sym}"
        # 如果已经是 US. 或 HK. 开头，直接返回
        if vt_symbol.startswith("US.") or vt_symbol.startswith("HK."):
            return vt_symbol
        # 默认按 market 补前缀
        prefix = "US." if market == "US" else "HK."
        return f"{prefix}{vt_symbol.replace('.SMART','').replace('.SEHK','')}"

    def _deploy_to_cta(self, strategy_name: str, class_name: str,
                       vt_symbol: str, params: dict, market: str) -> bool:
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
        else:
            if strategy_name in cta.strategies:
                logger.warning(f"[StrategyEngine] {strategy_name} 已在 CTA 引擎中但不在 self._deployed 中，移除后重新部署")
                try:
                    cta.stop_strategy(strategy_name)
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

            # ★ FIX: 部署成功后，向 SubscriptionManager 注册订阅（记录配额）
            if self.sub_manager:
                futu_symbol = self._to_futu_symbol(vt_symbol, market)
                if futu_symbol:
                    # 基础订阅类型：QUOTE（策略运行时需要实时报价）
                    self.sub_manager.subscribe(futu_symbol, ["QUOTE"])
                    logger.info(f"[StrategyEngine] ✅ 已向 SubManager 注册订阅: {futu_symbol} QUOTE")
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
        try:
            return target_me.get_engine("CtaStrategy")
        except Exception as e:
            logger.error(f"[StrategyEngine] 获取 {market} CTA 引擎失败: {e}")
            return None

    def boot(self, configs: list = None, operator: str = "system") -> dict:
        if configs is None:
            configs = self.db.get_all_strategies(enabled_only=True)
        logger.info(f"[Boot] ═══ 策略启动流程（双引擎）═══ 共 {len(configs)} 个")
        if not configs:
            logger.error("[Boot] ❌ strategy_config 表中没有启用策略")
            return {"all_pass": False, "deployed": [], "failed": []}
        results = {}
        deployed = []
        failed = []
        skipped = 0
        for cfg in configs:
            name = cfg["strategy_name"]
            current_hash = self._compute_params_hash(cfg.get("params", {}))
            if name in self._deployed and self._deployed[name]["params_hash"] == current_hash:
                logger.debug(f"[Boot] {name} 已部署且参数未变，跳过")
                skipped += 1
                results[name] = {"deployed": True, "skipped": True}
                deployed.append(name)
                continue
            result = self._validate_and_deploy(cfg, operator=operator)
            results[name] = result
            if result.get("deployed"):
                deployed.append(name)
                self._deployed[name] = {
                    "params_hash": current_hash,
                    "updated_at": cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    "status": "RUNNING",
                    "status_msg": "正常运行中"
                }
            else:
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

    def _resolve_class(self, class_name: str):
        real_name = self.CLASS_NAME_ALIAS.get(class_name, class_name)
        module_path = self.CLASS_MODULE_MAP.get(real_name)
        if not module_path:
            module_path = f"strategies.equity.{real_name.lower()}"
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, real_name, None)
            if cls is None:
                cls = getattr(mod, class_name, None)
            if cls is None:
                logger.error(f"[StrategyEngine] 模块 {module_path} 中未找到类 {real_name}")
                return None, None
            return cls, real_name
        except Exception as e:
            logger.error(f"[StrategyEngine] 类解析失败 {class_name}: {e}")
            return None, None

    def _normalize_vt_symbol(self, vt_symbol: str, market: str) -> str:
        if not vt_symbol or not vt_symbol.strip():
            return ""
        vt_symbol = vt_symbol.strip()
        if '.' in vt_symbol:
            symbol, _, exchange_str = vt_symbol.partition('.')
            if exchange_str == 'HK':
                return f"{symbol}.{Exchange.SEHK.value}"
            elif exchange_str == 'US':
                return f"{symbol}.{Exchange.SMART.value}"
            return vt_symbol
        exchange = Exchange.SEHK if market == "HK" else Exchange.SMART
        return f"{vt_symbol}.{exchange.value}"

    def _parse_strategy_name(self, strategy_name: str) -> Tuple[Optional[str], Optional[str]]:
        parts = strategy_name.split('_')
        for i, part in enumerate(parts):
            if part in ('HK', 'US'):
                market = part
                symbol = parts[i + 1] if i + 1 < len(parts) else None
                return symbol, market
        return None, None

    # ==================== 新增方法 ====================
    def get_all_strategies(self) -> List[Any]:
        """返回所有已部署的策略对象列表"""
        return list(self.strategies.values())
    # =================================================

    def start_all(self):
        for label, me in [("US", self.main_us), ("HK", self.main_hk)]:
            cta = self._get_cta_engine(label)
            if not cta:
                continue
            for name in list(cta.strategies.keys()):
                try:
                    s = cta.strategies[name]
                    if not getattr(s, 'trading', False) and getattr(s, 'inited', False):
                        cta.start_strategy(name)
                        self._set_status(name, "RUNNING", "手动启动")
                except Exception as e:
                    logger.warning(f"[StrategyEngine] 启动 {name} 失败: {e}")

    def stop_all(self):
        for label, me in [("US", self.main_us), ("HK", self.main_hk)]:
            cta = self._get_cta_engine(label)
            if not cta:
                continue
            for name in list(cta.strategies.keys()):
                try:
                    cta.stop_strategy(name)
                    cta.remove_strategy(name)
                    self._set_status(name, "STOPPED", "手动停止")
                except Exception as e:
                    logger.warning(f"[StrategyEngine] 停止 {name} 失败: {e}")
        self._deployed.clear()
        self.strategies.clear()

    def check_and_reload_changed(self, operator: str = "system") -> List[str]:
        db_configs = self.db.get_all_strategies(enabled_only=True)
        db_map = {cfg["strategy_name"]: cfg for cfg in db_configs}
        to_process = []
        for name, cfg in db_map.items():
            if name not in self._deployed:
                to_process.append((name, "add"))
            else:
                current_hash = self._compute_params_hash(cfg.get("params", {}))
                if self._deployed[name]["params_hash"] != current_hash:
                    to_process.append((name, "update"))
        db_names = set(db_map.keys())
        to_delete = set(self._deployed.keys()) - db_names
        processed = []
        for name in to_delete:
            self._remove_strategy(name, operator)
            processed.append(name)
        for name, action in to_process:
            cfg = db_map[name]
            if action == "add":
                logger.info(f"[HotReload] 发现新策略: {name}")
            else:
                logger.info(f"[HotReload] 策略参数变化: {name}")
            result = self._validate_and_deploy(cfg, operator=operator)
            if result.get("deployed"):
                self._deployed[name] = {
                    "params_hash": self._compute_params_hash(cfg.get("params", {})),
                    "updated_at": cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    "status": "RUNNING",
                    "status_msg": "热加载部署"
                }
            else:
                logger.warning(f"[HotReload] {name} 部署失败")
            processed.append(name)
        if processed:
            logger.info(f"[HotReload] 处理完成: {processed}")
        return processed

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

    def add_strategy(self, strategy_name: str, class_name: str,
                     vt_symbol: str, market: str,
                     params: dict, source: str = "manual",
                     modifier: str = "system") -> bool:
        self.db.save_strategy(strategy_name, class_name, vt_symbol, market, params,
                              source=source, modifier=modifier)
        cfg = self.db.get_strategy(strategy_name)
        if not cfg:
            return False
        result = self._validate_and_deploy(cfg, operator=modifier)
        if result.get("deployed"):
            self._deployed[strategy_name] = {
                "params_hash": self._compute_params_hash(cfg.get("params", {})),
                "updated_at": cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "status": "RUNNING",
                "status_msg": "手动添加"
            }
            return True
        return False

    def remove_strategy(self, strategy_name: str, operator: str = "system") -> bool:
        return self._remove_strategy(strategy_name, operator)

    def _remove_strategy(self, strategy_name: str, operator: str) -> bool:
        # ★ FIX: 移除前先取消订阅
        if self.sub_manager and strategy_name in self.strategies:
            strategy_obj = self.strategies[strategy_name]
            vt_symbol = getattr(strategy_obj, 'vt_symbol', '')
            market = 'US'  # 简化，可从策略对象获取
            if vt_symbol:
                futu_symbol = self._to_futu_symbol(vt_symbol, market)
                if futu_symbol:
                    self.sub_manager.unsubscribe(futu_symbol, ["QUOTE"])
                    logger.info(f"[StrategyEngine] ✅ 已从 SubManager 注销订阅: {futu_symbol}")

        for label, me in [("US", self.main_us), ("HK", self.main_hk)]:
            cta = self._get_cta_engine(label)
            if cta and strategy_name in cta.strategies:
                try:
                    cta.stop_strategy(strategy_name)
                    cta.remove_strategy(strategy_name)
                    self._set_status(strategy_name, "REMOVED", f"手动移除 (by {operator})")
                    logger.info(f"[StrategyEngine] ✅ {strategy_name} 已从 {label} CTA 引擎移除")
                except Exception as e:
                    logger.error(f"[StrategyEngine] 移除 {strategy_name} 失败: {e}")
                    return False
        self._deployed.pop(strategy_name, None)
        self.strategies.pop(strategy_name, None)
        return True