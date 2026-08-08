# -*- coding: utf-8 -*-
"""
main.py - Apollo AI Trader v3.8.4
================================================
变更记录：
  v3.8.4 - 新增 PipelineRunner 模块，统一 pipeline 流程编排
           - 启动时调用 pipeline_runner.run(market=...) 替代原函数
           - SchedulerJobs 注入 pipeline_runner 替代原 pipeline
           - StrategyEngine 移除所有手动持仓接管代码
           - 修复 SubscriptionManager v3.8.3+ 兼容问题
  v3.8.1 - 启动时双市场流水线并行（ThreadPoolExecutor）
           - 诊股/Regime/部署三阶段各市场内部并行
           - 保留 v3.8.0 全部功能，零回退
  ★ Strict - 移除所有配置加载的 try-except 和硬编码默认值
  ★ Fix   - StockSelector 调用参数修正为 quote_ctx=
"""
import json
import logging
import os
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.setting import SETTINGS
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_futu import FutuGateway

from core.machine_registry import MachineRegistry
from core.subscription_manager import SubscriptionManager
from core.remote_controller import RemoteController
from core.duallink import DualLink
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener
from core.order_manager import OrderManager, TradeSource, LiveTradeSource, SimTradeSource
from core.position_manager import PositionManager
from core.user_manager import UserManager
from core.account_manager import AccountManager
from core.risk_manager import RiskManager
from core.strategy_lifecycle_manager import StrategyLifecycleManager
from core.performance_tracker import PerformanceTracker
from ai.stock_diagnosis import StockDiagnosis
from core.regime_predictor import AdaptiveRegimePredictor
from core.strategy_engine import StrategyEngine
from core.pipeline_runner import PipelineRunner
from core.scheduler_jobs import SchedulerJobs       # <-- 新增导入

# ========== Logging ==========
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-15s | %(levelname)-8s: %(message)s",
    handlers=[
        logging.FileHandler("logs/main.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("Main")

# ========== Config (Strict) ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_config() -> dict:
    """严格模式：只读第一个存在的配置文件，失败直接抛出异常"""
    candidates = [
        os.path.join(BASE_DIR, "config", "system_config.json"),
        os.path.join(BASE_DIR, "system_config.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            log.info(f"✅ 配置已加载: {path}")
            return cfg
    raise FileNotFoundError(f"未找到配置文件，候选路径: {candidates}")

CONFIG = _load_config()

def _cfg(key):
    """直接返回配置值，缺失时抛出 KeyError"""
    return CONFIG[key]

ACCOUNT_POLL_INTERVAL = _cfg("account_poll_interval")
HOST = _cfg("opend_host")
PORT = _cfg("opend_port")
ENV  = _cfg("trade_env")
PASSWORD = _cfg("futu_password")

log.info(f"Host: [{HOST}:{PORT}] env={ENV}")

# ==================== main ====================

def main():
    # ===== 0. 数据库路径 =====
    db_path = CONFIG.get("db_path") or os.path.join(BASE_DIR, "data", "history.db")
    db_dir = os.path.dirname(db_path) or "."
    os.makedirs(db_dir, exist_ok=True)

    for f in [db_path, db_path + "-wal", db_path + "-shm"]:
        if os.path.exists(f):
            try: os.remove(f); log.info(f"Removed old db: {f}")
            except: pass

    # ===== 1. Machine Registry =====
    registry = MachineRegistry()
    log.info(f"Machine: {registry.summary()}")

    # ===== 2. Data feed =====
    SETTINGS["datafeed.name"] = "localdata"
    SETTINGS["datafeed.username"] = ""
    SETTINGS["datafeed.password"] = ""
    log.info("Data feed: localdata")

    # ===== 3. 双 MainEngine =====
    ev_us = EventEngine()
    me_us = MainEngine(ev_us)
    me_us.add_app(CtaStrategyApp)
    ev_hk = EventEngine()
    me_hk = MainEngine(ev_hk)
    me_hk.add_app(CtaStrategyApp)
    log.info("US/HK MainEngine ready")

    # ===== 4. Futu gateways =====
    gw_us = FutuGateway(ev_us, "FUTU_US")
    me_us.gateways["FUTU_US"] = gw_us
    gw_hk = FutuGateway(ev_hk, "FUTU_HK")
    me_hk.gateways["FUTU_HK"] = gw_hk
    gw_us.account_poll_interval = ACCOUNT_POLL_INTERVAL
    gw_hk.account_poll_interval = ACCOUNT_POLL_INTERVAL
    gw_us.connect({"地址": HOST, "端口": PORT, "市场": "US", "环境": ENV, "密码": PASSWORD})
    gw_hk.connect({"地址": HOST, "端口": PORT, "市场": "HK", "环境": ENV, "密码": PASSWORD})
    log.info("FUTU gateways connecting...")

    # ===== 5. 等 quote_ctx 可用 =====
    timeout = CONFIG.get("contract_timeout", 180)
    deadline = time.time() + timeout
    us_ready = hk_ready = False
    while time.time() < deadline:
        if not us_ready and gw_us.quote_ctx is not None: us_ready = True; log.info("US quote_ctx available")
        if not hk_ready and gw_hk.quote_ctx is not None: hk_ready = True; log.info("HK quote_ctx available")
        if us_ready and hk_ready: break
        time.sleep(0.5)
    else:
        log.warning("quote_ctx wait timeout, forcing proceed")
        us_ready = hk_ready = True
    log.info(f"Contract ready: US={us_ready}, HK={hk_ready}")

    # ===== 6. Database =====
    from core.db_manager import DBManager
    db = DBManager(db_path)
    log.info(f"DB initialized: {db_path}")

    # ===== 7. UserManager =====
    user_manager = UserManager(db)
    try:
        from core.user_manager import UserRole
        if not user_manager.db.user_exists("admin"):
            ok, msg = user_manager.create_user(
                username="admin", password=CONFIG.get("remote_password", "admin123"),
                role=UserRole.ADMIN, email="admin@apollo.local",
                initial_capital=1000000.0,
            )
            log.info(f"{'✅' if ok else '⚠️'} 默认管理员: {msg}")
    except Exception as e:
        log.warning(f"⚠️ UserManager 初始化异常: {e}")

    # ===== 8. Matcher & Advisor =====
    from core.strategy_matcher import StrategyMatcher
    matcher = StrategyMatcher()
    from ai.param_advisor import ParamAdvisor
    advisor = ParamAdvisor(db=db)

    # ===== 9. KlineProvider =====
    from core.kline_provider import KlineProvider
    kp = KlineProvider(quote_ctx=gw_us.quote_ctx)

    # ===== 10. AdaptiveRegimePredictor =====
    regime_predictor = AdaptiveRegimePredictor(
        quote_ctx=gw_us.quote_ctx, config=CONFIG.get("regime", {}), db=db,
    )
    log.info("AdaptiveRegimePredictor (v4.0) initialized")

    # ===== 11. Subscription Manager =====
    sub_manager = SubscriptionManager(max_quota=CONFIG.get("subscription_quota", 300))
    sub_manager.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)
    log.info("SubscriptionManager created")

    # ===== 12. Strategy Engine =====
    strategy_engine = StrategyEngine(
        main_us=me_us, main_hk=me_hk,
        db=db, config=CONFIG, quote_ctx=gw_us.quote_ctx,
        advisor=advisor, regime_predictor=regime_predictor,
    )
    strategy_engine.sub_manager = sub_manager
    strategy_engine.kline_provider = kp
    strategy_engine.matcher = matcher
    log.info("StrategyEngine created (v3.8.4)")

    # ===== 13. PositionManager =====
    position_manager = PositionManager()
    log.info("PositionManager created")

    # ===== 14. OrderManager =====
    if ENV.upper() == "REAL":
        trade_source_us = LiveTradeSource(gw_us, poll_interval=2.0)
        trade_source_hk = LiveTradeSource(gw_hk, poll_interval=2.0)
        log.info("TradeSource: LiveTradeSource (实盘模式)")
    else:
        trade_source_us = SimTradeSource(gw_us, poll_interval=2.0)
        trade_source_hk = SimTradeSource(gw_hk, poll_interval=2.0)
        log.info("TradeSource: SimTradeSource (模拟盘模式)")

    om = OrderManager(
        gateways={"FUTU_US": gw_us, "FUTU_HK": gw_hk},
        account_equity=CONFIG.get("account_equity", 100000.0),
        trade_source=trade_source_us,
    )
    om.position_manager = position_manager
    om.start()
    log.info("OrderManager created (v3.8.4)")

    # ===== 15. AccountManager =====
    account_manager = AccountManager(db_manager=db, main_engines={"US": gw_us, "HK": gw_hk})
    log.info("AccountManager created")

    # ===== 16. RiskManager =====
    risk_manager = None
    try:
        risk_manager = RiskManager(config=CONFIG.get("risk", {}))
        log.info("✅ RiskManager created")
    except Exception as e:
        log.warning(f"⚠️ RiskManager 不可用: {e}")

    # ===== 17. LifecycleManager =====
    lifecycle_manager = StrategyLifecycleManager(
        db_manager=db, risk_manager=risk_manager,
        order_manager=om, account_manager=account_manager,
        user_manager=user_manager,
    )
    lifecycle_manager.set_strategy_engine(strategy_engine)
    log.info("✅ LifecycleManager created")

    strategy_engine.lifecycle_manager = lifecycle_manager
    strategy_engine.account_manager = account_manager
    strategy_engine.risk_manager = risk_manager
    strategy_engine.user_manager = user_manager

    # ===== 18. Performance Tracker =====
    perf_tracker = PerformanceTracker(db=db, strategy_engine=strategy_engine)
    log.info("PerformanceTracker created")

    # ===== 19. DualLink =====
    dual_link = DualLink(main_us=me_us, main_hk=me_hk, db=db, config=CONFIG)
    log.info("DualLink created")

# ===== 20. Remote Controller (v3.8.4 修复注入) =====
    remote = RemoteController(
        db=db, notifier=None, config=CONFIG,
        account_manager=account_manager,
        order_manager=om,
    )
    remote.set_main_engines(me_us, me_hk)       # ★ 关键：注入双引擎网关
    remote.set_strategy_engine(strategy_engine)
    log.info(f"RemoteController created (gateways={list(remote._gateways.keys())})")

    # ===== 21. Telegram Notifier =====
    tg_token = CONFIG.get("telegram_token", "")
    tg_chat_id = CONFIG.get("telegram_chat_id", "")
    notifier = TelegramNotifier(token=tg_token, chat_id=tg_chat_id, machine_registry=registry)
    remote.notifier = notifier
    log.info("TelegramNotifier created")

    # ===== 22. Telegram Command Listener =====
    cmd_listener = TelegramCommandListener(
        token=tg_token, chat_id=tg_chat_id,
        controller=remote, poll_interval=CONFIG.get("telegram_poll_interval", 3.0),
    )
    log.info("TelegramCommandListener created")

    # ===== 23. 启动 TradeSource 轮询 =====
    trade_source_us.start()
    if ENV.upper() == "REAL": trade_source_hk.start()

    # ===== 24. 注册网关回调 =====
    om.start_account_polling(interval=ACCOUNT_POLL_INTERVAL)
    gw_us.register_position_callback(position_manager.sync_from_gateway)
    gw_hk.register_position_callback(position_manager.sync_from_gateway)
    log.info("Gateway callbacks registered")

    # ===== 25. 🚀 创建 PipelineRunner =====
    pipeline_runner = PipelineRunner(
        strategy_engine=strategy_engine,
        db=db,
        config=CONFIG,
        kp=kp,
        regime_predictor=regime_predictor,
        quote_ctx_us=gw_us.quote_ctx,
        quote_ctx_hk=gw_hk.quote_ctx,
    )
    log.info("PipelineRunner created (v3.8.4)")

    # ===== 26. 🚀 双市场并行 Pipeline（启动时执行）=====
    deployed_accumulator = []

    log.info("🚀 启动双市场并行流水线...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(pipeline_runner.run, "US"): "US",
            executor.submit(pipeline_runner.run, "HK"): "HK",
        }
        for f in as_completed(futures):
            m = futures[f]
            try:
                f.result()
                log.info(f"[Pipeline] {m} 完成")
            except Exception as e:
                log.error(f"[Pipeline] {m} 异常: {e}")
                log.error(traceback.format_exc())

    # 获取部署列表（从 strategy_engine 的 _deployed 中）
    deployed_accumulator = list(strategy_engine._deployed.keys())
    us_count = sum(1 for n in deployed_accumulator if "US" in str(n))
    hk_count = len(deployed_accumulator) - us_count

    # ===== 27. 注入 OrderManager + PerfTracker =====
    for strat_name in deployed_accumulator:
        obj = strategy_engine.strategies.get(strat_name)
        if obj:
            obj.order_manager = om
            obj.perf_tracker = perf_tracker

    # ===== 28. 配额审计 =====
    sub_manager.audit_quota()

    # ===== 29. 热加载 =====
    strategy_engine.start_hot_reload(interval=CONFIG.get("hot_reload_interval", 300))

    # ===== 30. DualLink 健康检查 =====
    dual_link.start()

    # ===== 31. Telegram 命令轮询 =====
    cmd_listener.start()

    # ===== 32. 🕐 定时任务调度器（注入 pipeline_runner）=====
    scheduler_jobs = SchedulerJobs(
        config=CONFIG, db=db,
        quote_ctx_us=gw_us.quote_ctx, quote_ctx_hk=gw_hk.quote_ctx,
        strategy_engine=strategy_engine, notifier=notifier,
        kline_provider=kp, regime_predictor=regime_predictor,
        sub_manager=sub_manager, lifecycle_manager=lifecycle_manager,
        account_manager=account_manager, user_manager=user_manager,
        risk_manager=risk_manager,
        pipeline_runner=pipeline_runner,
    )
    scheduler_jobs.start_scheduler()
    log.info("SchedulerJobs started (v3.8.4, pipeline_runner injected)")

    # ===== 33. PerformanceTracker 启动 =====
    perf_tracker.start()

    # ===== 34. 首次资金同步 =====
    try:
        account_manager.sync_all_positions("SYSTEM")
        summary = account_manager.get_capital_summary("SYSTEM")
        log.info(f"💰 资金摘要: {summary}")
    except Exception as e:
        log.error(f"⚠️ 资金同步失败: {e}")

    # ===== 35. 启动完成摘要 =====
    machine_id = registry.machine_id
    bot_name = CONFIG.get("bot_name", "default")
    log.info(
        f"🚀 Apollo v3.8.4 startup complete | [{bot_name}][{machine_id}][STANDALONE] "
        f"name=standalone-{machine_id[:4]} uptime=0h0m | "
        f"US={us_count} HK={hk_count} | "
        f"perf_tracker=ON | scheduler=SchedulerJobs | "
        f"lifecycle=ON | account_mgr=ON | risk_mgr={'ON' if risk_manager else 'OFF'}"
    )

    # ===== 36. 主线程保活 =====
    try:
        while True: time.sleep(10)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        scheduler_jobs.stop_scheduler()
        strategy_engine.stop_hot_reload()
        strategy_engine.stop_all()
        trade_source_us.stop()
        if ENV.upper() == "REAL": trade_source_hk.stop()
        om.stop_account_polling()
        om.stop()
        perf_tracker.stop()
        gw_us.close()
        gw_hk.close()
        log.info("Bye.")


if __name__ == "__main__":
    main()