"""
main.py - Apollo AI Trader v3.0.0 (with Apollo OrderManager integration)
"""
import json
import logging
import os
import sys
import time
import threading

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.setting import SETTINGS
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_futu import FutuGateway

from core.machine_registry import MachineRegistry
from core.subscription_manager import SubscriptionManager
from core.subscription_plan import (
    apply_subscription_plan,
    _strategy_required_subtypes,
)
from core.remote_controller import RemoteController
from core.duallink import DualLink
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener
from core.order_manager import OrderManager  # <<< NEW IMPORT

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

log.info(f"Host: [{CONFIG.get('opend_host','127.0.0.1')}:{CONFIG.get('opend_port',11111)}]")


def main():
    # ===== 0. Remove old database =====
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

    # ===== 3. Twin engines =====
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

    us_setting = {"address": host, "port": port, "market": "US", "env": env}
    hk_setting = {"address": host, "port": port, "market": "HK", "env": env}

    gw_us.connect(us_setting)
    gw_hk.connect(hk_setting)
    log.info("Connecting to Futu...")

    timeout = CONFIG.get("connection_timeout", 15)
    for i in range(timeout):
        us_rdy = gw_us.quote_ctx is not None
        hk_rdy = gw_hk.quote_ctx is not None
        if us_rdy and hk_rdy:
            log.info("US/HK quote ready")
            break
        time.sleep(1)
    else:
        log.error("Quote context timeout")
        ev_us.stop()
        ev_hk.stop()
        sys.exit(1)

    # ===== 5. Subscription Manager =====
    sub_manager = SubscriptionManager(
        max_quota=CONFIG.get("subscription", {}).get("max_quota", 300)
    )
    sub_manager.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)
    sub_manager.register_gateway("FUTU_US", gw_us)
    sub_manager.register_gateway("FUTU_HK", gw_hk)
    log.info("SubscriptionManager ready")

    # ===== 6. Database =====
    from core.db_manager import DBManager
    db = DBManager(db_path)
    log.info(f"DB ready: {db_path}")

    # ===== 7. Regime Trainer =====
    regime_trainer = None
    try:
        from core.regime_trainer import RegimeTrainer
        regime_trainer = RegimeTrainer(config=CONFIG.get("regime", {}), db=db)
        if hasattr(regime_trainer, "start"):
            regime_trainer.start()
        log.info("RegimeTrainer ready")
    except Exception as e:
        log.warning(f"RegimeTrainer skipped: {e}")

    # ===== 8. Strategy Engine =====
    advisor = None
    try:
        from ai.param_advisor import ParamAdvisor
        advisor = ParamAdvisor(db)
        log.info("ParamAdvisor ready")
    except Exception as e:
        log.warning(f"ParamAdvisor skipped: {e}")

    matcher = None
    try:
        from core.strategy_matcher import StrategyMatcher
        matcher = StrategyMatcher(db_path=db_path)
        log.info("StrategyMatcher ready")
    except Exception as e:
        log.warning(f"StrategyMatcher skipped: {e}")

    from core.strategy_engine import StrategyEngine
    strategy_engine = StrategyEngine(
        main_us=me_us,
        main_hk=me_hk,
        db=db,
        config=CONFIG,
        advisor=advisor,
    )
    strategy_engine.sub_manager = sub_manager
    log.info("StrategyEngine ready")

    # ===== 8.1 Apollo OrderManager (NEW) =====
    order_manager = OrderManager(
        gateways={"FUTU_US": gw_us, "FUTU_HK": gw_hk},
        query_interval=2.0,
        account_equity=CONFIG.get("account_equity", 100000.0)
    )
    order_manager.start()
    strategy_engine.order_manager = order_manager
    log.info("Apollo OrderManager started (smart routing + position + sizing)")

    # ===== 8.5 Kline Provider =====
    from core.kline_provider import KlineProvider
    kp_us = KlineProvider(quote_ctx=gw_us.quote_ctx, market="US",
                          max_retries=3, request_interval=0.35)
    kp_hk = KlineProvider(quote_ctx=gw_hk.quote_ctx, market="HK",
                          max_retries=3, request_interval=0.35)
    log.info("KlineProvider ready")

    if hasattr(strategy_engine, '_contract_ready'):
        for _ in range(30):
            if strategy_engine._contract_ready:
                log.info("Contracts ready, preloading...")
                break
            time.sleep(1)
        else:
            log.warning("Contract wait timeout")
    else:
        log.info("Waiting 15s for contracts...")
        time.sleep(15)

    base_subtypes = _strategy_required_subtypes({"GridStrategy"})
    log.info(f"Base subscription: {base_subtypes}")

    us_symbols = CONFIG.get("universe", {}).get("US", [])
    hk_symbols = CONFIG.get("universe", {}).get("HK", [])

    kp_us.preload_for_subscription_plan(us_symbols, base_subtypes)
    kp_hk.preload_for_subscription_plan(hk_symbols, base_subtypes)
    log.info("K-line preload done")

    # ===== 9. Pipeline =====
    log.info("=== Pipeline start ===")
    selected = []
    try:
        from ai.stock_selector import AIStockSelector
        selector = AIStockSelector(
            quote_ctx=gw_us.quote_ctx, db=db, market="US", kline_provider=kp_us
        )
        selected = selector.select()
        log.info(f"Selected {len(selected)} stocks")
    except Exception as e:
        log.warning(f"Selection failed: {e}")

    if selected:
        try:
            from ai.stock_diagnosis import StockDiagnosis
            diag = StockDiagnosis(quote_ctx=gw_us.quote_ctx, db=db, kline_provider=kp_us)
            for item in selected:
                symbol = item.get("vt_symbol", item.get("code", ""))
                if not symbol:
                    continue
                try:
                    result = diag.diagnose(symbol)
                    log.info(f"Diagnosis {symbol}: {result}")
                except Exception as e:
                    log.warning(f"Diagnosis failed {symbol}: {e}")
        except Exception as e:
            log.warning(f"Diagnosis module error: {e}")

        if regime_trainer:
            regime_trainer.kp = kp_us
            for item in selected:
                symbol = item.get("vt_symbol", item.get("code", ""))
                if not symbol:
                    continue
                try:
                    regime_result = regime_trainer.predict(symbol)
                    log.info(f"Regime {symbol}: {regime_result}")
                except Exception as e:
                    log.warning(f"Regime failed {symbol}: {e}")

        try:
            from core.strategy_generator import StrategyGenerator
            generator = StrategyGenerator(
                quote_ctx=gw_us.quote_ctx, db_path=db_path,
                matcher=matcher, param_advisor=advisor, db=db
            )
            count = generator.generate_from_selector(selected)
            log.info(f"Generated {count} strategies")
        except Exception as e:
            log.error(f"Strategy generation failed: {e}")

        deployed_result = strategy_engine.boot(operator="pipeline")
        deployed_count = len(deployed_result.get("deployed", [])) if isinstance(deployed_result, dict) else deployed_result
        log.info(f"Deployed {deployed_count} strategies")

        # ===== 8.2 Inject OrderManager into every deployed strategy (NEW) =====
        for strategy in strategy_engine.get_all_strategies():
            strategy.order_manager = order_manager
        log.info(f"Injected OrderManager into {deployed_count} strategies")

        sub_manager.audit_quota()

        if hasattr(strategy_engine, 'start_hot_reload'):
            strategy_engine.start_hot_reload(interval=60)
            log.info("Hot reload started")
    else:
        log.info("No selection, skip pipeline")
    log.info("=== Pipeline done ===")

    # ===== 10. DualLink =====
    duallink = None
    try:
        duallink = DualLink(main_us=me_us, main_hk=me_hk, db=db)
        duallink.start()
        log.info("DualLink started")
    except Exception as e:
        log.warning(f"DualLink skipped: {e}")

    # ===== 11. Telegram =====
    token = CONFIG.get("telegram_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")

    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id, machine_registry=registry)
        try:
            notifier.send_message("Apollo v3.0.0 started")
            log.info("Telegram connected")
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")

        controller = RemoteController(db=db, notifier=notifier, config=CONFIG)
        controller.set_main_engines(me_us, me_hk)
        controller.set_strategy_engine(strategy_engine)
        controller.set_registry(registry)

        listener = TelegramCommandListener(
            token, chat_id, controller,
            poll_interval=CONFIG.get("telegram_poll_interval", 3.0)
        )
        listener.start()
        log.info("Telegram command listener started")

    # ===== 12. Scheduler =====
    scheduler = None
    try:
        from core.scheduler_jobs import SchedulerJobs
        scheduler = SchedulerJobs(config=CONFIG)
        if hasattr(scheduler, "start"):
            scheduler.start()
        log.info("Scheduler started")
    except Exception as e:
        log.warning(f"Scheduler skipped: {e}")

    # ===== 13. Background maintenance =====
    def periodic():
        while True:
            time.sleep(60)
            try:
                sub_manager.process_unsub_queue()
            except Exception:
                pass
            try:
                registry.heartbeat()
            except Exception:
                pass
            if duallink and hasattr(duallink, "health"):
                try:
                    log.info(f"[DualLink] {duallink.health()}")
                except Exception:
                    pass
            if duallink and hasattr(duallink, "reconnect_if_needed"):
                try:
                    duallink.reconnect_if_needed()
                except Exception:
                    pass
            if int(time.time()) % 600 < 60:
                try:
                    sub_manager.audit_quota()
                except Exception:
                    pass

    threading.Thread(target=periodic, daemon=True).start()

    # ===== 14. Main loop =====
    log.info(f"Startup complete | {registry.summary()} | twin-engine + smart routing")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted (Ctrl+C)")
    finally:
        log.info("Shutting down...")
        try:
            strategy_engine.stop_all()
        except Exception:
            pass
        order_manager.stop()  # NEW
        if scheduler and hasattr(scheduler, "stop"):
            try:
                scheduler.stop()
            except Exception:
                pass
        if duallink and hasattr(duallink, "stop"):
            try:
                duallink.stop()
            except Exception:
                pass
        try:
            ev_us.stop()
        except Exception:
            pass
        try:
            ev_hk.stop()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        log.info("Safe shutdown complete")


if __name__ == "__main__":
    main()