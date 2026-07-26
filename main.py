"""
main.py — Apollo AI Trader v2.8.2 主入口（最终版）
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
from vnpy_futu import FutuGateway

from core.machine_registry import MachineRegistry
from core.subscription_manager import SubscriptionManager
from core.subscription_plan import apply_subscription_plan
from core.remote_controller import RemoteController
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener

# ========== 日志配置 ==========
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

# ========== 配置加载 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "system_config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

log.info(f"🖥️ 本机标识: [{CONFIG.get('opend_host','127.0.0.1')}:{CONFIG.get('opend_port',11111)}]")


def main():
    # 1. 初始化机器注册
    registry = MachineRegistry(CONFIG.get("cluster", {}))
    log.info(f"🏷️ 机器注册: {registry.summary()}")

    # 2. 配置本地数据服务
    SETTINGS["datafeed.name"] = "localdata"
    SETTINGS["datafeed.username"] = ""
    SETTINGS["datafeed.password"] = ""
    log.info("📊 数据服务: 使用本地数据库 (vnpy_localdata)")

    # 3. 创建主引擎并加载 CTA 策略应用
    ev = EventEngine()
    me = MainEngine(ev)

    from vnpy_ctastrategy import CtaStrategyApp
    me.add_app(CtaStrategyApp)
    log.info("✅ CTA 策略应用已加载")

    # 4. 连接双网关并等待行情就绪
    host = CONFIG["opend_host"]
    port = CONFIG["opend_port"]
    env = CONFIG["trade_env"]

    gw_us = FutuGateway(ev, "FUTU_US")
    gw_hk = FutuGateway(ev, "FUTU_HK")

    me.gateways["FUTU_US"] = gw_us
    me.gateways["FUTU_HK"] = gw_hk

    us_setting = {"地址": host, "端口": port, "市场": "US", "环境": env}
    hk_setting = {"地址": host, "端口": port, "市场": "HK", "环境": env}

    gw_us.connect(us_setting)
    log.info("✅ US 网关连接中...")
    gw_hk.connect(hk_setting)
    log.info("✅ HK 网关连接中...")

    timeout = CONFIG.get("connection_timeout", 10)
    for i in range(timeout):
        us_ready = gw_us.quote_ctx is not None
        hk_ready = gw_hk.quote_ctx is not None
        if us_ready and hk_ready:
            log.info("✅ US 行情就绪")
            log.info("✅ HK 行情就绪")
            break
        time.sleep(1)
    else:
        if gw_us.quote_ctx is None:
            log.error(f"❌ US 行情超时（{timeout}秒内未就绪）")
        if gw_hk.quote_ctx is None:
            log.error(f"❌ HK 行情超时（{timeout}秒内未就绪）")
        sys.exit(1)

    # 5. 初始化订阅管理器并执行订阅计划
    sub_manager = SubscriptionManager(
        max_quota=CONFIG.get("subscription", {}).get("max_quota", 300)
    )
    sub_manager.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)
    apply_subscription_plan(sub_manager, CONFIG)
    sub_manager.audit_quota()

    # 6. 初始化数据库
    from core.db_manager import DBManager
    db = DBManager(CONFIG.get("db_path") or os.path.join(BASE_DIR, "data", "apollo.db"))

    # 7. 初始化 RegimeTrainer（精确：只传 config）
    regime_trainer = None
    try:
        from core.regime_trainer import RegimeTrainer
        regime_trainer = RegimeTrainer(config=CONFIG.get("regime", {}))
        if hasattr(regime_trainer, "start"):
            regime_trainer.start()
        log.info("✅ RegimeTrainer 已初始化")
    except ImportError:
        log.info("ℹ️ RegimeTrainer 不可用，跳过")
    except Exception as e:
        log.warning(f"⚠️ RegimeTrainer 初始化失败(非致命): {e}")

    # 8. 初始化策略引擎（精确：双引擎 + db + config）
    strategy_engine = None
    try:
        from core.strategy_engine import StrategyEngine
        strategy_engine = StrategyEngine(
            main_us=me,
            main_hk=me,
            db=db,
            config=CONFIG,
        )
        log.info("✅ StrategyEngine 已初始化")
    except ImportError:
        log.warning("⚠️ StrategyEngine 不可用")
    except Exception as e:
        log.warning(f"⚠️ StrategyEngine 初始化失败(非致命): {e}")

    # ===== 9. 选股 + 策略匹配（精确适配 StrategyMatcher 真实接口）=====
    try:
        from ai.stock_selector import AIStockSelector
        from core.strategy_matcher import StrategyMatcher

        selector = AIStockSelector(quote_ctx=gw_us.quote_ctx, db=db, market="US")
        selected = selector.select()

        import json as _json
        log.info(f"🔍 DEBUG selected sample: {_json.dumps(selected[:2], ensure_ascii=False, indent=2)[:500]}")

        # ★ 精确构造 StrategyMatcher：只需 db_path（字符串路径）★
        db_path = CONFIG.get("db_path") or os.path.join(BASE_DIR, "data", "apollo.db")
        matcher = StrategyMatcher(db_path=db_path)

        # 为每只选中的股票分配最佳策略
        matched_results = []
        for item in selected:
            symbol = item.get("vt_symbol", item.get("code", ""))
            # 判断市场：包含 .SEHK 或 HK. 则为 HK，否则默认 US
            market = "HK" if ".SEHK" in symbol or symbol.startswith("HK.") else "US"
            best_strategy = matcher.select_strategy(symbol, market)
            matched_results.append({
                "symbol": symbol,
                "strategy": best_strategy,
                "score": item.get("score", 0),
            })
            log.info(f"🎯 {symbol} → {best_strategy} score={item.get('score', 0)}")

        # 如果需要，可以将匹配结果存入数据库或传递给策略引擎
        # （此处仅记录日志，后续可由 StrategyEngine 从数据库加载策略配置）
    except Exception as e:
        log.warning(f"⚠️ 选股/策略匹配失败(非致命): {e}")

    # 10. 启动策略引擎
    if strategy_engine:
        try:
            boot_result = strategy_engine.boot(operator="system")
            log.info(f"🚀 策略启动结果: {boot_result}")
        except Exception as e:
            log.warning(f"⚠️ 策略启动失败: {e}")

    # 11. Telegram 远程控制
    token = CONFIG.get("telegram_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")

    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id, machine_registry=registry)
        if notifier.send_message("🚀 Apollo v2.8.2 启动完成"):
            log.info("✅ Telegram Bot连接成功")

        controller = RemoteController(db=db, notifier=notifier, config=CONFIG)
        controller.set_main_engine(me)
        if strategy_engine:
            controller.set_strategy_engine(strategy_engine)
        controller.set_registry(registry)

        listener = TelegramCommandListener(
            token, chat_id, controller,
            poll_interval=CONFIG.get("telegram_poll_interval", 3.0)
        )
        listener.start()
        log.info("✅ Telegram 远程控制已启动")

    # 12. 定时任务
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
            if int(time.time()) % 600 < 60:
                try:
                    sub_manager.audit_quota()
                except Exception:
                    pass

    threading.Thread(target=periodic, daemon=True).start()

    # 13. 主循环
    log.info(f"🚀 启动完成 | 标识: {registry.summary()}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("👋 手动中断 (Ctrl+C)...")
    finally:
        if strategy_engine:
            try:
                strategy_engine.stop_all()
            except Exception:
                pass
        ev.stop()
        log.info("✅ 已安全关闭")


if __name__ == "__main__":
    main()