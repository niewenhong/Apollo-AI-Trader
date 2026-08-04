"""
main.py - Apollo AI Trader v3.3.2
变更：
  v3.3.2 - generator.generate() 补传 market=market 参数
           防止 HK Pipeline 误用默认 'US' 清除对方市场
"""
import json
import logging
import os
import time
import threading
import traceback

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
from core.order_manager import OrderManager

# 顶层导入
from ai.stock_diagnosis import StockDiagnosis

# ========== Logging ==========
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/main.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("Main")

# ========== Config ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "system_config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

ACCOUNT_POLL_INTERVAL = CONFIG.get("account_poll_interval", 60)
log.info(f"Host: [{CONFIG.get('opend_host','127.0.0.1')}:{CONFIG.get('opend_port',11111)}]")


def _run_pipeline_for_market(market, gw_us, gw_hk, kp, strategy_engine, db, db_path,
                             matcher, advisor, sub_manager, deployed_accumulator):
    """对一个市场跑完整流水线：选股 → 诊股 → (regime) → 策略生成 → 部署"""
    log.info(f"=== Pipeline [{market}] start ===")

    # ---- 1. 选股（不自动诊股） ----
    selected = []
    try:
        from ai.stock_selector import StockSelector
        ctx = gw_us.quote_ctx if market == "US" else gw_hk.quote_ctx
        selector = StockSelector(
            quote_ctx=ctx,
            db=db,
            kline_provider=kp,
            config=CONFIG,
        )
        selected = selector.run(markets=[market])
        log.info(f"[{market}] Selected {len(selected)} stocks")
    except Exception as e:
        log.error(f"[{market}] Selection failed: {e}")
        log.error(traceback.format_exc())
        return 0

    if not selected:
        log.info(f"[{market}] No selection, skip pipeline")
        return 0

    # ---- 2. 诊股（独立循环，避免卡住选股） ----
    ctx_for_diag = gw_us.quote_ctx if market == "US" else gw_hk.quote_ctx
    diag = StockDiagnosis(quote_ctx=ctx_for_diag, db=db, kline_provider=kp)
    for item in selected:
        symbol = item.get("vt_symbol", "")
        if not symbol:
            continue
        try:
            result = diag.diagnose(symbol)
            summary = result.get("summary", "")
            item.setdefault("extra", {})["diagnosis"] = summary
            log.info(f"[{market}] Diagnosis {symbol}: {summary}")
        except Exception as e:
            log.warning(f"[{market}] Diagnosis failed {symbol}: {e}")

    # ---- 3. Regime 预测（可选） ----
    if hasattr(strategy_engine, 'regime_trainer') and strategy_engine.regime_trainer:
        try:
            strategy_engine.regime_trainer.kp = kp
            for item in selected:
                symbol = item.get("vt_symbol", "")
                if not symbol:
                    continue
                try:
                    regime_result = strategy_engine.regime_trainer.predict(symbol)
                    log.info(f"[{market}] Regime {symbol}: {regime_result}")
                except Exception as e:
                    log.warning(f"[{market}] Regime failed {symbol}: {e}")
        except Exception as e:
            log.warning(f"[{market}] Regime trainer error: {e}")

    # ---- 4. 策略生成 ----
    try:
        from core.strategy_generator import StrategyGenerator
        generator = StrategyGenerator(
            quote_ctx=gw_us.quote_ctx if market == "US" else gw_hk.quote_ctx,
            db_path=db_path,
            kline_provider=kp,
            config=CONFIG,
        )
        regime_map = {}
        for item in selected:
            vt = item.get("vt_symbol", "")
            if vt:
                regime_map[vt] = {
                    "regime": item.get("regime", "range"),
                    "confidence": item.get("confidence", 0.5),
                    "iv_percentile": item.get("iv_percentile", 0.5),
                }
        # v3.3.2: 补传 market=market
        count = generator.generate(selected, regime_map, market=market)
        log.info(f"[{market}] Generated {count} strategies")
    except Exception as e:
        log.error(f"[{market}] Strategy generation failed: {e}")
        log.error(traceback.format_exc())
        return 0

    # ---- 5. 部署 ----
    deployed_result = strategy_engine.boot(operator=f"pipeline_{market}")
    if isinstance(deployed_result, dict):
        deployed_list = deployed_result.get("deployed", [])
    else:
        deployed_list = list(deployed_result) if deployed_result else []
    deployed_count = len(deployed_list)
    log.info(f"[{market}] Deployed {deployed_count} strategies")

    deployed_accumulator.extend(deployed_list)
    sub_manager.audit_quota()
    log.info(f"=== Pipeline [{market}] done ===")
    return deployed_count


def main():
    # ===== 0. 删旧库（调试期保留） =====
    db_path = CONFIG.get("db_path") or os.path.join(BASE_DIR, "data", "history.db")
    db_dir = os.path.dirname(db_path) or "."
    for f in [db_path, db_path + "-wal", db_path + "-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
                log.info(f"Removed old db: {f}")
            except Exception:
                pass
    os.makedirs(db_dir, exist_ok=True)

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
    host = CONFIG["opend_host"]
    port = CONFIG["opend_port"]
    env = CONFIG["trade_env"]

    gw_us = FutuGateway(ev_us, "FUTU_US")
    me_us.gateways["FUTU_US"] = gw_us
    gw_hk = FutuGateway(ev_hk, "FUTU_HK")
    me_hk.gateways["FUTU_HK"] = gw_hk

    gw_us.account_poll_interval = ACCOUNT_POLL_INTERVAL
    gw_hk.account_poll_interval = ACCOUNT_POLL_INTERVAL

    gw_us.connect({"地址": host, "端口": port, "市场": "US", "环境": env})
    gw_hk.connect({"地址": host, "端口": port, "市场": "HK", "环境": env})
    log.info("FUTU gateways connecting...")

    # ===== 5. 等 quote_ctx 可用 =====
    timeout = CONFIG.get("contract_timeout", 180)
    deadline = time.time() + timeout
    us_ready = hk_ready = False
    while time.time() < deadline:
        if not us_ready and gw_us.quote_ctx is not None:
            us_ready = True
            log.info("US quote_ctx available")
        if not hk_ready and gw_hk.quote_ctx is not None:
            hk_ready = True
            log.info("HK quote_ctx available")
        if us_ready and hk_ready:
            break
        time.sleep(0.5)
    else:
        log.warning("quote_ctx wait timeout after %ds, forcing proceed", timeout)
        us_ready = True
        hk_ready = True
    log.info(f"Contract ready: US={us_ready}, HK={hk_ready}")

    # ===== 6. Database =====
    from core.db_manager import DBManager
    db = DBManager(db_path)
    log.info(f"DB initialized: {db_path}")

    # ===== 7. Matcher & Advisor =====
    from core.strategy_matcher import StrategyMatcher
    matcher = StrategyMatcher()
    from ai.param_advisor import ParamAdvisor
    advisor = ParamAdvisor(db=db)

    # ===== 8. KlineProvider =====
    from core.kline_provider import KlineProvider
    kp = KlineProvider(quote_ctx=gw_us.quote_ctx)

    # ===== 9. Subscription Manager =====
    sub_manager = SubscriptionManager(
        max_quota=CONFIG.get("subscription_quota", 300)
    )
    sub_manager.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)
    log.info("SubscriptionManager created")

    # ===== 10. Strategy Engine =====
    from core.strategy_engine import StrategyEngine
    strategy_engine = StrategyEngine(
        main_us=me_us, main_hk=me_hk,
        db=db, config=CONFIG,
        quote_ctx=gw_us.quote_ctx,
        advisor=advisor,
    )
    strategy_engine.sub_manager = sub_manager
    strategy_engine.kline_provider = kp
    strategy_engine.matcher = matcher
    log.info("StrategyEngine created")

    # ===== 11. OrderManager =====
    om = OrderManager(
        gateways={"FUTU_US": gw_us, "FUTU_HK": gw_hk},
        account_equity=CONFIG.get("account_equity", 100000.0),
    )
    om.start()
    log.info("OrderManager created")

    # ===== 12. Performance Tracker =====
    from core.performance_tracker import PerformanceTracker
    perf_tracker = PerformanceTracker(db=db, strategy_engine=strategy_engine)
    log.info("PerformanceTracker created")

    # ===== 13. DualLink =====
    dual_link = DualLink(
        main_us=me_us,
        main_hk=me_hk,
        db=db,
        config=CONFIG,
    )
    log.info("DualLink created")

    # ===== 14. Remote Controller =====
    remote = RemoteController(
        db=db,
        notifier=None,
        config=CONFIG,
    )
    remote.set_strategy_engine(strategy_engine)
    log.info("RemoteController created")

    # ===== 15. Telegram Notifier =====
    tg_token = CONFIG.get("telegram_token", "")
    tg_chat_id = CONFIG.get("telegram_chat_id", "")
    notifier = TelegramNotifier(
        token=tg_token,
        chat_id=tg_chat_id,
        machine_registry=registry,
    )
    remote.notifier = notifier
    log.info("TelegramNotifier created")

    # ===== 16. Telegram Command Listener =====
    cmd_listener = TelegramCommandListener(
        token=tg_token,
        chat_id=tg_chat_id,
        controller=remote,
        poll_interval=CONFIG.get("telegram_poll_interval", 3.0),
    )
    log.info("TelegramCommandListener created")

    # ===== 17. 🚀 启动选股流水线 =====
    deployed_accumulator = []

    us_count = _run_pipeline_for_market(
        "US", gw_us, gw_hk, kp, strategy_engine, db, db_path,
        matcher, advisor, sub_manager, deployed_accumulator,
    )
    hk_count = _run_pipeline_for_market(
        "HK", gw_us, gw_hk, kp, strategy_engine, db, db_path,
        matcher, advisor, sub_manager, deployed_accumulator,
    )

    # ===== 18. 注入 OrderManager + PerfTracker =====
    for strat_name in deployed_accumulator:
        obj = strategy_engine.strategies.get(strat_name)
        if obj:
            obj.order_manager = om
            obj.perf_tracker = perf_tracker
            log.info(f"Injected OrderManager + PerfTracker into {strat_name}")

    # ===== 19. 配额审计 =====
    sub_manager.audit_quota()

    # ===== 20. 热加载 =====
    strategy_engine.start_hot_reload(interval=CONFIG.get("hot_reload_interval", 300))

    # ===== 21. DualLink 健康检查 =====
    dual_link.start()

    # ===== 22. Telegram 命令轮询 =====
    cmd_listener.start()

    # ===== 23. 定时任务 =====
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=lambda: strategy_engine.check_and_reload_changed(operator="scheduler"),
        trigger="interval",
        hours=CONFIG.get("scheduler_reload_hours", 24),
        id="daily_reload", replace_existing=True,
    )
    scheduler.start()

    # ===== 24. 启动完成摘要 =====
    machine_id = registry.machine_id
    bot_name = CONFIG.get("bot_name", "default")
    log.info(
        f"🚀 Apollo v3.3.2 startup complete | [{bot_name}][{machine_id}][STANDALONE] "
        f"name=standalone-{machine_id[:4]} uptime=0h0m | "
        f"US={us_count} HK={hk_count} | perf_tracker=ON"
    )

    # ===== 25. 主线程保活 =====
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        strategy_engine.stop_hot_reload()
        strategy_engine.stop_all()
        om.stop()
        scheduler.shutdown(wait=False)
        log.info("Bye.")


if __name__ == "__main__":
    main()
