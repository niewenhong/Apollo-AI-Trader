"""
core/strategy_engine.py - Apollo Trader v2.7.0
严格遵循 VnPy 标准: vt_symbol 格式为 symbol.Exchange (00700.SEHK, AAPL.SMART)
"""
import importlib, os, json, time, threading, logging, traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Exchange
from vnpy_ctastrategy.engine import CtaEngine
from core.db_manager import CustomDBManager
from core.prelive_gate import PreliveGate
from ai.param_advisor import ParamAdvisor

logger = logging.getLogger("StrategyEngine")


class StrategyEngine:
    def __init__(self, us_engine: MainEngine, hk_engine: MainEngine,
                 db: CustomDBManager, config: dict = None):
        self.us_engine = us_engine
        self.hk_engine = hk_engine
        self.db = db
        self.config = config or {}
        self.telegram_bot = None
        self.strategies: Dict[str, object] = {}
        self._deployed: Dict[str, float] = {}
        gate_cfg = self.config.get("prelive_gate", {})
        self.prelive_gate = PreliveGate(db, thresholds=gate_cfg.get("thresholds"))
        self.advisor = ParamAdvisor(db)
        self.hot_reload_interval = gate_cfg.get("hot_reload_interval", 60)
        self.backtest_days = gate_cfg.get("backtest_days", 60)
        self.backtest_interval = gate_cfg.get("backtest_interval", "1m")

        # 初始化 CTA 引擎（标准 VnPy 流程）
        self._init_cta_engines()
        self._load_strategies()

    def _init_cta_engines(self):
        """初始化 US 和 HK 的 CTA 引擎"""
        for market in ["US", "HK"]:
            engine = self._get_cta_engine(market)
            if engine is None:
                logger.warning(f"[StrategyEngine] 无法获取 {market} 的 CTA 引擎")
                continue
            try:
                engine.init_engine()
                logger.info(f"[StrategyEngine] {market} CTA 引擎初始化完成")

                custom_modules = [
                    "strategies.equity.multi_indicator_strategy",
                    "strategies.equity.dual_thrust_strategy",
                ]
                for module in custom_modules:
                    try:
                        engine.load_strategy_class_from_module(module)
                        logger.info(f"[StrategyEngine] 已加载策略模块 {module}")
                    except Exception as e:
                        logger.warning(f"[StrategyEngine] 加载 {module} 失败: {e}")

                logger.info(f"[StrategyEngine] {market} 已注册策略类: {list(engine.classes.keys())}")
            except Exception as e:
                logger.error(f"[StrategyEngine] 初始化 {market} CTA 引擎异常: {e}\n{traceback.format_exc()}")

    # ── CTA引擎获取 ──
    def _get_cta_engine(self, market: str) -> Optional[CtaEngine]:
        main = self.us_engine if market == "US" else self.hk_engine
        if main is None:
            return None
        try:
            return main.get_engine("CtaStrategy")
        except Exception:
            return None

    def _get_cta_engine_for_strategy(self, strategy_name: str) -> Optional[CtaEngine]:
        cfg = self.db.get_strategy(strategy_name)
        if not cfg:
            return None
        return self._get_cta_engine(cfg["market"])

    # ── vt_symbol 标准化为 VnPy 格式: symbol.Exchange ──
    def _normalize_vt_symbol(self, vt_symbol: str, market: str) -> str:
        """
        转换为 VnPy 标准格式: symbol.Exchange
        港股 -> 00700.SEHK, 美股 -> AAPL.SMART
        
        输入支持多种格式:
        - 空字符串: 返回空（由调用方处理）
        - "00700" -> "00700.SEHK"
        - "HK.00700" -> "00700.SEHK"  (富途格式转 VnPy)
        - "AAPL" -> "AAPL.SMART"
        - "US.AAPL" -> "AAPL.SMART"
        - "00700.SEHK" -> "00700.SEHK" (已是标准格式)
        """
        if not vt_symbol or not vt_symbol.strip():
            return ""
        
        vt_symbol = vt_symbol.strip()
        
        # 如果包含点号
        if '.' in vt_symbol:
            symbol, _, exchange_str = vt_symbol.partition('.')
            # 富途格式: HK.00700 或 US.AAPL -> 转换为 VnPy 标准
            if exchange_str == 'HK':
                return f"{symbol}.{Exchange.SEHK.value}"
            elif exchange_str == 'US':
                return f"{symbol}.{Exchange.SMART.value}"
            # 已经是 VnPy 格式 (如 00700.SEHK)
            return vt_symbol
        
        # 纯代码，根据市场补全交易所
        exchange = Exchange.SEHK if market == 'HK' else Exchange.SMART
        return f"{vt_symbol}.{exchange.value}"

    # ── 从策略名称中提取 symbol 和 market ──
    def _parse_strategy_name(self, strategy_name: str):
        """
        从策略名称中提取 symbol 和 market
        例如: MultiInd_HK_00700_Enhanced -> symbol=00700, market=HK
              MultiInd_US_AAPL_Enhanced -> symbol=AAPL, market=US
        """
        parts = strategy_name.split('_')
        market = None
        symbol = None
        for i, part in enumerate(parts):
            if part in ('HK', 'US'):
                market = part
                # 尝试提取 symbol（HK/US 后面的部分）
                if i + 1 < len(parts):
                    symbol = parts[i + 1]
                break
        return symbol, market

    # ── 策略加载 ──
    def _load_strategies(self):
        db_configs = self.db.get_all_strategies(enabled_only=True)
        if db_configs:
            logger.info(f"[StrategyEngine] 从数据库加载 {len(db_configs)} 个策略")
            for cfg in db_configs:
                # ★ 关键修复：如果 vt_symbol 为空，从策略名称推断并持久化到数据库
                if not cfg.get("vt_symbol"):
                    symbol, market = self._parse_strategy_name(cfg["strategy_name"])
                    if symbol and market:
                        # 持久化修复
                        self.db.save_strategy(
                            cfg["strategy_name"], cfg["class_name"], symbol, market,
                            cfg.get("params", {}), source="auto_fix", modifier="system:fix_empty_vt_symbol"
                        )
                        cfg["vt_symbol"] = symbol
                        cfg["market"] = market
                        logger.warning(f"[StrategyEngine] 策略 {cfg['strategy_name']} 的 vt_symbol 为空，"
                                     f"从名称推断出 symbol={symbol}, market={market} 并已修复数据库")
                
                # 标准化 vt_symbol
                cfg["vt_symbol"] = self._normalize_vt_symbol(cfg["vt_symbol"], cfg.get("market", "US"))
                self._instantiate(cfg)
            return
        
        json_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'strategies.json')
        if not os.path.exists(json_path):
            logger.warning(f"[StrategyEngine] 数据库和 {json_path} 均无策略配置")
            return
        logger.info(f"[StrategyEngine] 数据库为空，从 {json_path} 加载并写入数据库")
        with open(json_path, 'r', encoding='utf-8') as f:
            json_configs = json.load(f)
        for cfg in json_configs:
            name = cfg.get('name', 'Unnamed')
            vt_symbol = cfg.get('vt_symbol', '')
            market = cfg.get('market', 'US')
            class_name = cfg.get('class', '')
            params = cfg.get('params', {})
            
            # 如果 vt_symbol 为空，从名称推断
            if not vt_symbol:
                symbol, m = self._parse_strategy_name(name)
                if symbol and m:
                    vt_symbol = symbol
                    market = m
            
            # 标准化
            vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
            
            self.db.save_strategy(name, class_name, vt_symbol, market, params,
                                  source='json_seed', modifier='system:bootstrap')
            db_cfg = self.db.get_strategy(name)
            if db_cfg:
                self._instantiate(db_cfg)
            logger.info(f"[StrategyEngine] JSON策略已加载并入库: {name}")

    def _instantiate(self, cfg: dict) -> Optional[object]:
        name = cfg["strategy_name"]
        class_name = cfg["class_name"]
        params = cfg.get("params", {})
        market = cfg.get("market", "US")
        vt_symbol = cfg["vt_symbol"]
        
        # 再次确保 vt_symbol 不为空
        if not vt_symbol:
            logger.error(f"[StrategyEngine] 策略 {name} 的 vt_symbol 为空，无法实例化")
            return None
        
        try:
            strategy_cls = self._resolve_class(class_name)
            if strategy_cls is None:
                logger.error(f"[StrategyEngine] 无法解析类: {class_name}")
                return None
            engine = self.us_engine if market == "US" else self.hk_engine
            cta = engine.get_engine("CtaStrategy")
            strategy = strategy_cls(
                cta_engine=cta,
                strategy_name=name,
                vt_symbol=vt_symbol,
                setting=params,
            )
            self.strategies[name] = strategy
            self._deployed[name] = cfg.get("updated_at", time.time())
            logger.info(f"[StrategyEngine] 策略已实例化: {name} ({market}), vt_symbol={vt_symbol}")
            return strategy
        except Exception as e:
            logger.error(f"[StrategyEngine] 实例化策略失败 {name}: {e}\n{traceback.format_exc()}")
            return None

    def _resolve_class(self, class_name: str):
        _known = {
            "MultiIndicatorStrategy": "strategies.equity.multi_indicator_strategy",
            "DualThrustStrategy": "strategies.equity.dual_thrust_strategy",
        }
        try:
            if class_name in _known:
                mod = importlib.import_module(_known[class_name])
                return getattr(mod, class_name)
            mod = importlib.import_module(f"strategies.equity.{class_name.lower()}")
            return getattr(mod, class_name, None)
        except Exception as e:
            logger.error(f"[StrategyEngine] 类解析失败 {class_name}: {e}")
            return None

    # ── 标准部署（VnPy 三步曲） ──
    def _deploy_to_cta(self, strategy_name, class_name, vt_symbol, params, market) -> bool:
        engine = self._get_cta_engine(market)
        if engine is None:
            logger.error(f"[Engine] 无法获取 {market} 的 CTA 引擎")
            return False

        # 标准化 vt_symbol 为 VnPy 格式
        vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
        if not vt_symbol:
            logger.error(f"[Engine] 策略 {strategy_name} 的 vt_symbol 为空，部署失败")
            return False
        
        logger.info(f"[Engine] 部署策略 {strategy_name}, vt_symbol={vt_symbol}")

        try:
            # 检查策略类是否已注册
            if class_name not in engine.classes:
                logger.error(f"[Engine] 策略类 '{class_name}' 未注册。"
                             f"已注册: {list(engine.classes.keys())}")
                return False

            # 如果策略已存在则先移除
            if strategy_name in engine.strategies:
                engine.stop_strategy(strategy_name)
                engine.remove_strategy(strategy_name)
                time.sleep(0.5)

            # ★ VnPy 标准三步曲
            engine.add_strategy(
                class_name=class_name,
                strategy_name=strategy_name,
                vt_symbol=vt_symbol,
                setting=params
            )
            engine.init_strategy(strategy_name)
            time.sleep(1)  # 等待异步初始化完成
            engine.start_strategy(strategy_name)

            self.strategies[strategy_name] = engine.strategies.get(strategy_name)
            logger.info(f"[Engine] ✅ 策略部署成功: {strategy_name}")
            return True
        except Exception as e:
            logger.error(f"[Engine] {strategy_name} 部署异常: {e}\n{traceback.format_exc()}")
            return False

    # ── 启动所有策略 ──
    def start_all(self):
        for name, strategy in list(self.strategies.items()):
            try:
                engine = self._get_cta_engine_for_strategy(name)
                if engine is None:
                    logger.warning(f"[StrategyEngine] 无法获取 {name} 的CTA引擎，跳过")
                    continue
                if name not in engine.strategies:
                    cfg = self.db.get_strategy(name)
                    if cfg:
                        ok = self._deploy_to_cta(
                            name, cfg["class_name"], cfg["vt_symbol"],
                            cfg.get("params", {}), cfg["market"]
                        )
                        if ok:
                            logger.info(f"[StrategyEngine] 已部署并启动: {name}")
                        else:
                            logger.warning(f"[StrategyEngine] 部署失败: {name}")
                else:
                    engine.start_strategy(name)
                    logger.info(f"[StrategyEngine] 已启动(已存在): {name}")
            except Exception as e:
                logger.error(f"[StrategyEngine] 启动 {name} 失败: {e}\n{traceback.format_exc()}")

    def stop_all(self):
        for market in ["US", "HK"]:
            engine = self._get_cta_engine(market)
            if not engine:
                continue
            for name in list(engine.strategies.keys()):
                try:
                    engine.stop_strategy(name)
                    engine.remove_strategy(name)
                except Exception as e:
                    logger.warning(f"[Engine] 停止 {name} 失败: {e}")
        self._deployed.clear()

    # ── 门禁启动 ──
    def boot(self, operator: str = "system") -> Dict:
        logger.info("[Engine] ═══ 开始策略启动流程（门禁验证）═══")
        configs = self.db.get_all_strategies(enabled_only=True)
        if not configs:
            logger.warning("[Engine] 数据库中没有启用策略")
            return {"all_pass": False, "deployed": [], "failed": []}
        results, deployed, failed = {}, [], []
        for cfg in configs:
            name = cfg["strategy_name"]
            try:
                result = self._validate_and_deploy(cfg, operator=operator)
                results[name] = result
                if result.get("deployed"):
                    deployed.append(name)
                    self._deployed[name] = cfg["updated_at"]
                else:
                    failed.append(name)
            except Exception as e:
                logger.error(f"[Engine] {name} 部署异常: {e}\n{traceback.format_exc()}")
                failed.append(name)
                results[name] = {"deployed": False, "reason": str(e)}
        summary = {"all_pass": len(failed) == 0, "deployed": deployed,
                   "failed": failed, "results": results}
        if summary["all_pass"]:
            logger.info(f"[Engine] ✅ 全部 {len(deployed)} 个策略通过门禁并部署")
        else:
            logger.warning(f"[Engine] ⚠️ 成功 {len(deployed)}, 失败 {len(failed)}")
        return summary

    def _validate_and_deploy(self, cfg: dict, operator: str = "system") -> dict:
        name = cfg["strategy_name"]
        vt_symbol = cfg["vt_symbol"]
        class_name = cfg["class_name"]
        params = cfg.get("params", {})
        market = cfg["market"]
        version = cfg.get("current_version", 1)
        
        # 确保 vt_symbol 不为空（同时持久化修复）
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
                logger.warning(f"[Engine] 策略 {name} 的 vt_symbol 为空，已修复并持久化")
        
        vt_symbol = self._normalize_vt_symbol(vt_symbol, market)
        
        try:
            strategy_cls = self._resolve_class(class_name)
            if strategy_cls is None:
                self.db.log_deploy(name, version, "deploy", operator, "failed", "类解析失败")
                return {"deployed": False, "reason": f"无法解析类: {class_name}"}
            suggested = self.advisor.suggest(vt_symbol, class_name, params)
            if suggested:
                params = {**params, **suggested}
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=self.backtest_days)
            gate_result = self.prelive_gate.validate(
                strategy_class_name=class_name,
                strategy_class=strategy_cls,
                vt_symbol=vt_symbol,
                setting={**params, "_version": version},
                modifier=operator,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                interval=self.backtest_interval,
            )
            if not gate_result["pass"]:
                reason = gate_result.get("reason", "未知原因")
                self.db.log_deploy(name, version, "deploy", operator, "failed", reason)
                logger.warning(f"[Engine] {name} 门禁未通过: {reason}")
                return {"deployed": False, "reason": reason, "gate": gate_result}
            if self._deploy_to_cta(name, class_name, vt_symbol, params, market):
                self.db.mark_deployed(name, version, operator)
                logger.info(f"[Engine] ✅ {name} 部署成功 (v{version})")
                return {"deployed": True, "version": version, "gate": gate_result}
            self.db.log_deploy(name, version, "deploy", operator, "failed", "CTA注册失败")
            return {"deployed": False, "reason": "CTA引擎注册失败"}
        except Exception as e:
            logger.error(f"[Engine] {name} 验证部署异常: {e}\n{traceback.format_exc()}")
            self.db.log_deploy(name, version, "deploy", operator, "failed", str(e))
            return {"deployed": False, "reason": f"异常: {str(e)}"}

    # ── 热加载 ──
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
                self._deployed[name] = cfg["updated_at"]
            else:
                logger.warning(f"[Engine] {name} 验证失败，保留旧版本运行")
        return processed

    def start_hot_reload(self, interval: int = None):
        if interval:
            self.hot_reload_interval = interval
        def _loop():
            logger.info(f"[Engine] 🔄 热加载已启动 (间隔 {self.hot_reload_interval}s)")
            while True:
                try:
                    changed = self.check_and_reload_changed(operator="hot_reload")
                    if changed:
                        logger.info(f"[Engine] 热加载处理: {changed}")
                except Exception as e:
                    logger.error(f"[Engine] 热加载异常: {e}")
                for _ in range(self.hot_reload_interval):
                    time.sleep(1)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return t

    # ── 回滚 ──
    def rollback(self, strategy_name: str, target_version: int, operator: str = "telegram") -> bool:
        cfg = self.db.get_strategy(strategy_name)
        if not cfg:
            return False
        old_params = self.db.get_param_version(cfg["vt_symbol"], cfg["class_name"], target_version)
        if not old_params:
            return False
        _, new_version = self.db.save_strategy(
            strategy_name, cfg["class_name"], cfg["vt_symbol"], cfg["market"],
            old_params, source="rollback", modifier=f"system:{operator}")
        cfg["params"] = old_params
        cfg["updated_at"] = time.time()
        result = self._validate_and_deploy(cfg, operator=f"rollback:{operator}")
        if result.get("deployed"):
            self._deployed[strategy_name] = time.time()
            self.db.log_deploy(strategy_name, new_version, "rollback", operator,
                               "success", f"回滚到 v{target_version}")
            return True
        return False

    # ── 增删策略 ──
    def add_strategy(self, strategy_name, class_name, vt_symbol, market,
                     params: dict, source="manual", modifier="system") -> bool:
        self.db.save_strategy(strategy_name, class_name, vt_symbol, market, params,
                              source=source, modifier=modifier)
        cfg = self.db.get_strategy(strategy_name)
        result = self._validate_and_deploy(cfg, operator=modifier)
        if result.get("deployed"):
            self._deployed[strategy_name] = cfg["updated_at"]
            return True
        return False

    def remove_strategy(self, strategy_name, operator="system") -> bool:
        return self._remove_strategy(strategy_name, operator)

    def _remove_strategy(self, strategy_name, operator) -> bool:
        cfg = self.db.get_strategy(strategy_name)
        if cfg:
            engine = self._get_cta_engine(cfg["market"])
            if engine and strategy_name in engine.strategies:
                try:
                    engine.stop_strategy(strategy_name)
                    engine.remove_strategy(strategy_name)
                except Exception as e:
                    logger.warning(f"[Engine] 移除 {strategy_name} 异常: {e}")
        self.db.disable_strategy(strategy_name)
        self.db.log_deploy(strategy_name, 0, "remove", operator, "success", "")
        self._deployed.pop(strategy_name, None)
        self.strategies.pop(strategy_name, None)
        return True

    # ── 查询 ──
    def list_active(self) -> List[dict]:
        return self.db.get_active_strategies()

    def list_all(self, enabled_only=False) -> List[dict]:
        return self.db.get_all_strategies(enabled_only=enabled_only)

    def get_param_history(self, strategy_name, limit=20) -> List[dict]:
        cfg = self.db.get_strategy(strategy_name)
        if not cfg:
            return []
        return self.db.get_param_history(cfg["vt_symbol"], cfg["class_name"], limit)

    # ── 事件回调 ──
    def on_bar(self, code: str, bar):
        for name, strategy in self.strategies.items():
            if getattr(strategy, 'code', None) == code:
                try:
                    strategy.on_bar(bar)
                except Exception as e:
                    logger.warning(f"[StrategyEngine] {name} on_bar 异常: {e}")

    def on_backtest_complete(self):
        for name, strategy in self.strategies.items():
            try:
                if hasattr(strategy, 'on_backtest_end'):
                    strategy.on_backtest_end()
            except Exception as e:
                logger.warning(f"[StrategyEngine] {name} on_backtest_end 异常: {e}")

    # ── 门禁配置 ──
    def set_gate_threshold(self, key: str, value: float):
        self.prelive_gate.set_threshold(key, value)

    def get_gate_report(self) -> str:
        return json.dumps(self.prelive_gate.get_thresholds(), indent=2)

    # ── 通知 ──
    def notify(self, level: str, msg: str, strategy_name: str = ""):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_msg = f"[{timestamp}] [{level}] {msg}"
        logger.log(level.upper(), full_msg)
        if self.db:
            try:
                self.db.log_event(timestamp, level, msg, strategy_name)
            except Exception:
                pass
        if self.telegram_bot and level in ("TRADE", "ERROR", "WARN"):
            try:
                self.telegram_bot.send_message(full_msg)
            except Exception:
                pass