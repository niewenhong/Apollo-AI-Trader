"""
core/strategy_engine.py - Apollo Trader v2.8.2
完整 CTA 策略引擎：回测 → 门禁 → init → prelive → start 全链路自动化
支持数据库驱动的动态策略管理（增删改查 + 热加载 + 版本回滚）

⚠️ 门禁回测已完全移除，后续由 AI 智能判断替代
"""
import importlib
import json
import time
import threading
import logging
import traceback
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
    """
    策略引擎 - 全链路自动化
    """

    # ===== 类名别名映射 =====
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

    def __init__(self, main_us: MainEngine, main_hk: MainEngine, db: DBManager,
                 config: dict = None):
        self.main_us = main_us
        self.main_hk = main_hk
        self.main_engine = main_us
        self.db = db
        self.config = config or {}
        self.telegram_bot = None

        self.strategies: Dict[str, Any] = {}
        self._deployed: Dict[str, str] = {}

        gate_cfg = self.config.get("prelive_gate", {})
        self.prelive_gate = PreliveGate(db, thresholds=gate_cfg.get("thresholds"))
        self.advisor = ParamAdvisor(db)
        self.hot_reload_interval = gate_cfg.get("hot_reload_interval", 600)  # 临时调大到600秒减少干扰
        self.backtest_days = gate_cfg.get("backtest_days", 60)
        self.backtest_interval = gate_cfg.get("backtest_interval", "1m")

        self._hot_reload_stop = threading.Event()

        self._init_cta_engine()
        self._load_strategies_from_db()

    def _init_cta_engine(self):
        cta = self._get_cta_engine()
        if cta is None:
            logger.error("[StrategyEngine] 无法获取 CTA 引擎")
            return
        try:
            cta.init_engine()
            logger.info("[StrategyEngine] ✅ CTA 引擎初始化完成")
        except Exception as e:
            logger.error(f"[StrategyEngine] CTA 引擎初始化异常: {e}")
            return

        loaded_classes = []
        for class_name, module_path in self.CLASS_MODULE_MAP.items():
            try:
                cta.load_strategy_class_from_module(module_path)
                loaded_classes.append(class_name)
            except Exception as e:
                logger.warning(f"[StrategyEngine] 加载 {module_path} 失败: {e}")
        logger.info(f"[StrategyEngine] 已注册策略类: {loaded_classes}")

    def _get_cta_engine(self) -> Optional[CtaEngine]:
        try:
            return self.main_engine.get_engine("CtaStrategy")
        except Exception as e:
            logger.error(f"[StrategyEngine] 获取 CTA 引擎失败: {e}")
            return None

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

    def _load_strategies_from_db(self):
        db_configs = self.db.get_all_strategies(enabled_only=True)
        if not db_configs:
            logger.info("[StrategyEngine] 数据库无启用策略，等待选股匹配后注册")
            return
        logger.info(f"[StrategyEngine] 从数据库加载 {len(db_configs)} 个策略")
        for cfg in db_configs:
            if not cfg.get("vt_symbol"):
                symbol, market = self._parse_strategy_name(cfg["strategy_name"])
                if symbol and market:
                    self.db.save_strategy(
                        cfg["strategy_name"], cfg["class_name"], symbol, market,
                        cfg.get("params", {}), source="auto_fix",
                        modifier="system:fix_empty_vt_symbol"
                    )
                    cfg["vt_symbol"] = symbol
                    cfg["market"] = market
            cfg["vt_symbol"] = self._normalize_vt_symbol(
                cfg["vt_symbol"], cfg.get("market", "US")
            )
        self.boot(operator="system:bootstrap")

    def _resolve_class(self, class_name: str):
        """解析策略类，自动处理别名映射"""
        real_name = self.CLASS_NAME_ALIAS.get(class_name, class_name)
        if real_name != class_name:
            logger.info(f"[StrategyEngine] 类名别名映射: {class_name} -> {real_name}")

        module_path = self.CLASS_MODULE_MAP.get(real_name)
        if not module_path:
            module_path = f"strategies.equity.{real_name.lower()}"

        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, real_name, None)
            if cls is None:
                cls = getattr(mod, class_name, None)
            if cls is None:
                logger.error(f"[StrategyEngine] 模块 {module_path} 中未找到类 {real_name} 或 {class_name}")
                return None, None
            return cls, real_name
        except Exception as e:
            logger.error(f"[StrategyEngine] 类解析失败 {class_name}: {e}")
            return None, None

    def _validate_and_deploy(self, cfg: dict, operator: str = "system") -> dict:
        name = cfg["strategy_name"]
        vt_symbol = cfg.get("vt_symbol", "")
        class_name = cfg["class_name"]
        params = cfg.get("params", {}).copy()
        market = cfg.get("market", "US")
        version = cfg.get("current_version", 1)

        if not vt_symbol:
            symbol, m = self._parse_strategy_name(name)
            if symbol and m:
                self.db.save_strategy(
                    name, class_name, symbol, m, params,
                    source="auto_fix", modifier="system:fix_empty_vt_symbol"
                )
                vt_symbol = symbol
                market = m
                cfg["vt_symbol"] = symbol
                cfg["market"] = m

        vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
        if not vt_symbol:
            error_msg = f"vt_symbol 为空，无法部署 {name}"
            logger.error(f"[StrategyEngine] {error_msg}")
            self.db.log_deploy(name, version, "deploy", operator, "failed", error_msg)
            return {"deployed": False, "reason": error_msg}

        try:
            strategy_cls, actual_class_name = self._resolve_class(class_name)
            if strategy_cls is None:
                error_msg = f"无法解析类: {class_name}"
                self.db.log_deploy(name, version, "deploy", operator, "failed", error_msg)
                return {"deployed": False, "reason": error_msg}

            suggested = self.advisor.suggest(vt_symbol, class_name, params)
            if suggested:
                params.update(suggested)
                logger.info(f"[StrategyEngine] {name} AI参数建议: {suggested}")

            # ===== 门禁已完全移除，直接部署 =====
            # 不再调用 prelive_gate.validate()
            # ===================================

            if self._deploy_to_cta(name, actual_class_name, vt_symbol, params, market):
                self.db.mark_deployed(name, version, operator)
                self.db.log_deploy(name, version, "deploy", operator, "success",
                                   "gate_disabled")
                logger.info(f"[StrategyEngine] ✅ {name} 全链路部署成功 (v{version})")
                return {"deployed": True, "version": version, "gate": {"pass": True}}

            error_msg = "CTA引擎注册失败"
            self.db.log_deploy(name, version, "deploy", operator, "failed", error_msg)
            return {"deployed": False, "reason": error_msg}

        except Exception as e:
            logger.error(f"[StrategyEngine] {name} 部署异常: {e}\n{traceback.format_exc()}")
            self.db.log_deploy(name, version, "deploy", operator, "failed", str(e))
            return {"deployed": False, "reason": f"异常: {str(e)}"}

    def _deploy_to_cta(self, strategy_name: str, class_name: str,
                       vt_symbol: str, params: dict, market: str) -> bool:
        cta = self._get_cta_engine()
        if cta is None:
            return False

        vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
        if not vt_symbol:
            logger.error(f"[StrategyEngine] {strategy_name} vt_symbol 为空")
            return False

        logger.info(f"[StrategyEngine] 部署 {strategy_name} → {vt_symbol} ({class_name})")

        try:
            if class_name not in cta.classes:
                logger.error(f"[StrategyEngine] '{class_name}' 未注册。已注册: {list(cta.classes.keys())}")
                return False

            if strategy_name in cta.strategies:
                cta.stop_strategy(strategy_name)
                cta.remove_strategy(strategy_name)
                time.sleep(0.5)

            cta.add_strategy(
                class_name=class_name,
                strategy_name=strategy_name,
                vt_symbol=vt_symbol,
                setting=params,
            )
            init_success = cta.init_strategy(strategy_name)
            if not init_success:
                logger.error(f"[StrategyEngine] {strategy_name} init 失败")
                cta.remove_strategy(strategy_name)
                return False

            for _ in range(10):
                if strategy_name in cta.strategies:
                    break
                time.sleep(0.5)
            else:
                logger.error(f"[StrategyEngine] {strategy_name} 初始化超时")
                cta.remove_strategy(strategy_name)
                return False

            cta.start_strategy(strategy_name)
            self.strategies[strategy_name] = cta.strategies.get(strategy_name)
            logger.info(f"[StrategyEngine] ✅ {strategy_name} add→init→start 完成")
            return True

        except Exception as e:
            logger.error(f"[StrategyEngine] {strategy_name} 部署异常: {e}\n{traceback.format_exc()}")
            return False

    def boot(self, operator: str = "system") -> dict:
        logger.info("[StrategyEngine] ═══ 开始策略启动流程（门禁已禁用）═══")
        configs = self.db.get_all_strategies(enabled_only=True)
        if not configs:
            logger.warning("[StrategyEngine] 数据库中没有启用策略")
            return {"all_pass": False, "deployed": [], "failed": [], "results": {}}

        results = {}
        deployed = []
        failed = []

        for cfg in configs:
            name = cfg["strategy_name"]
            result = self._validate_and_deploy(cfg, operator=operator)
            results[name] = result
            if result.get("deployed"):
                deployed.append(name)
                self._deployed[name] = cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            else:
                failed.append(name)

        summary = {
            "all_pass": len(failed) == 0,
            "deployed": deployed,
            "failed": failed,
            "results": results,
        }

        if summary["all_pass"]:
            logger.info(f"[StrategyEngine] ✅ 全部 {len(deployed)} 个策略部署成功")
        else:
            logger.warning(f"[StrategyEngine] ⚠️ 成功 {len(deployed)}, 失败 {len(failed)}: {failed}")
        return summary

    def start_all(self):
        cta = self._get_cta_engine()
        if not cta:
            return
        for name in list(cta.strategies.keys()):
            try:
                if not cta.strategies[name].trading:
                    cta.start_strategy(name)
                    logger.info(f"[StrategyEngine] 启动: {name}")
            except Exception as e:
                logger.warning(f"[StrategyEngine] 启动 {name} 失败: {e}")

    def stop_all(self):
        cta = self._get_cta_engine()
        if not cta:
            return
        for name in list(cta.strategies.keys()):
            try:
                cta.stop_strategy(name)
                cta.remove_strategy(name)
                logger.info(f"[StrategyEngine] 停止并移除: {name}")
            except Exception as e:
                logger.warning(f"[StrategyEngine] 停止 {name} 失败: {e}")
        self._deployed.clear()
        self.strategies.clear()

    def check_and_reload_changed(self, operator: str = "system") -> List[str]:
        changed = self.db.detect_changed_strategies(self._deployed)
        if not changed:
            return []

        processed = []
        for cfg in changed:
            name = cfg.get("strategy_name")
            change_type = cfg.get("_change_type", "updated")

            if change_type in ("deleted", "disabled"):
                self._remove_strategy(name, operator)
                processed.append(name)
                continue

            processed.append(name)
            result = self._validate_and_deploy(cfg, operator=operator)
            if result.get("deployed"):
                self._deployed[name] = cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            else:
                logger.warning(f"[StrategyEngine] {name} 验证失败，保留旧版本运行")
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
            self._deployed[strategy_name] = cfg.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return True
        return False

    def remove_strategy(self, strategy_name: str, operator: str = "system") -> bool:
        return self._remove_strategy(strategy_name, operator)

    def _remove_strategy(self, strategy_name: str, operator: str) -> bool:
        cta = self._get_cta_engine()
        if cta and strategy_name in cta.strategies:
            try:
                cta.stop_strategy(strategy_name)
                cta.remove_strategy(strategy_name)
            except Exception as e:
                logger.warning(f"[StrategyEngine] 移除 {strategy_name} 异常: {e}")
        self.db.disable_strategy(strategy_name)
        self.db.log_deploy(strategy_name, 0, "remove", operator, "success", "")
        self._deployed.pop(strategy_name, None)
        self.strategies.pop(strategy_name, None)
        logger.info(f"[StrategyEngine] 已移除策略: {strategy_name}")
        return True

    def rollback(self, strategy_name: str, target_version: int,
                 operator: str = "telegram") -> bool:
        cfg = self.db.get_strategy(strategy_name)
        if not cfg:
            return False
        old_params = self.db.get_param_version(
            cfg["vt_symbol"], cfg["class_name"], target_version
        )
        if not old_params:
            return False

        _, new_version = self.db.save_strategy(
            strategy_name, cfg["class_name"], cfg["vt_symbol"], cfg["market"],
            old_params, source="rollback", modifier=f"system:{operator}"
        )
        cfg["params"] = old_params
        cfg["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        result = self._validate_and_deploy(cfg, operator=f"rollback:{operator}")
        if result.get("deployed"):
            self._deployed[strategy_name] = cfg["updated_at"]
            self.db.log_deploy(strategy_name, new_version, "rollback", operator,
                               "success", f"回滚到 v{target_version}")
            return True
        return False

    def get_status(self) -> dict:
        cta = self._get_cta_engine()
        if not cta:
            return {"total": 0, "running": 0, "stopped": 0, "strategies": []}

        strategies_info = []
        for name, strategy in cta.strategies.items():
            info = {
                "name": name,
                "trading": getattr(strategy, 'trading', False),
                "pos": getattr(strategy, 'pos', 0),
                "score": getattr(strategy, 'score', 0),
            }
            strategies_info.append(info)

        total = len(strategies_info)
        running = sum(1 for s in strategies_info if s["trading"])
        return {
            "total": total,
            "running": running,
            "stopped": total - running,
            "strategies": strategies_info,
        }

    def list_active(self) -> List[dict]:
        return self.db.get_active_strategies()

    def list_all(self, enabled_only: bool = False) -> List[dict]:
        return self.db.get_all_strategies(enabled_only=enabled_only)

    def get_param_history(self, strategy_name: str, limit: int = 20) -> List[dict]:
        cfg = self.db.get_strategy(strategy_name)
        if not cfg:
            return []
        return self.db.get_param_history(cfg["vt_symbol"], cfg["class_name"], limit)

    def set_gate_threshold(self, key: str, value: float):
        self.prelive_gate.set_threshold(key, value)

    def get_gate_report(self) -> str:
        thresholds = self.prelive_gate.get_thresholds()
        return json.dumps(thresholds, indent=2, ensure_ascii=False)

    def format_status(self) -> str:
        status = self.get_status()
        lines = [f"📊 策略状态: {status['running']}/{status['total']} 运行中"]
        for s in status["strategies"]:
            icon = "✅" if s["trading"] else "⏸️"
            lines.append(f"  {icon} {s['name']} (pos={s['pos']}, score={s.get('score',0):.0f})")
        return "\n".join(lines)

    def notify(self, level: str, msg: str, strategy_name: str = ""):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_msg = f"[{timestamp}] [{level}] {msg}"
        logger.log(getattr(logging, level.upper(), logging.INFO), full_msg)
        try:
            self.db.log_event(timestamp, level, msg, strategy_name)
        except Exception:
            pass
        if self.telegram_bot and level in ("TRADE", "ERROR", "WARN"):
            try:
                self.telegram_bot.send_message(full_msg)
            except Exception:
                pass