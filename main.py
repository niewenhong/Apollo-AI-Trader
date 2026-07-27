"""
main.py — Apollo AI Trader v2.8.3 主入口（双引擎版）
变更说明：
  - 引入双 EventEngine + 双 MainEngine（me_us / me_hk 完全独立）
  - 美股网关 FUTU_US 只注册到 me_us，港股网关 FUTU_HK 只注册到 me_hk
  - DualLink 传入双引擎，独立健康检查 US/HK 链路
  - SchedulerJobs 按原逻辑启动（如有）
  - StrategyEngine 传入 main_us + main_hk
  - RemoteController 通过 set_main_engines(me_us, me_hk) 注入双引擎
  - 主循环退出时安全停止双 EventEngine + 双引擎
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
from core.duallink import DualLink
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

    # ===== 3. 双引擎初始化（v2.8.3 核心改造）=====
    # 美股事件引擎 + 主引擎
    ev_us = EventEngine()
    me_us = MainEngine(ev_us)
    from vnpy_ctastrategy import CtaStrategyApp
    me_us.add_app(CtaStrategyApp)
    log.info("✅ US MainEngine 已创建并加载 CTA 策略应用")

    # 港股事件引擎 + 主引擎
    ev_hk = EventEngine()
    me_hk = MainEngine(ev_hk)
    me_hk.add_app(CtaStrategyApp)
    log.info("✅ HK MainEngine 已创建并加载 CTA 策略应用")

    # ===== 4. 连接双网关并等待行情就绪 =====
    host = CONFIG["opend_host"]
    port = CONFIG["opend_port"]
    env = CONFIG["trade_env"]

    # 美股网关只注册到 me_us
    gw_us = FutuGateway(ev_us, "FUTU_US")
    me_us.gateways["FUTU_US"] = gw_us

    # 港股网关只注册到 me_hk
    gw_hk = FutuGateway(ev_hk, "FUTU_HK")
    me_hk.gateways["FUTU_HK"] = gw_hk

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
        # 清理
        ev_us.stop()
        ev_hk.stop()
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

    # 7. 初始化 RegimeTrainer
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

    # ===== 8. 初始化策略引擎（双引擎注入）=====
    strategy_engine = None
    try:
        from core.strategy_engine import StrategyEngine
        strategy_engine = StrategyEngine(
            main_us=me_us,
            main_hk=me_hk,
            db=db,
            config=CONFIG,
        )
        log.info("✅ StrategyEngine 已初始化（双引擎模式）")
    except ImportError:
        log.warning("⚠️ StrategyEngine 不可用")
    except Exception as e:
        log.warning(f"⚠️ StrategyEngine 初始化失败(非致命): {e}")

    # ===== 9. 选股 + 策略匹配 =====
    try:
        from ai.stock_selector import AIStockSelector
        from core.strategy_matcher import StrategyMatcher

        selector = AIStockSelector(quote_ctx=gw_us.quote_ctx, db=db, market="US")
        selected = selector.select()

        import json as _json
        log.info(f"🔍 DEBUG selected sample: {_json.dumps(selected[:2], ensure_ascii=False, indent=2)[:500]}")

        db_path = CONFIG.get("db_path") or os.path.join(BASE_DIR, "data", "apollo.db")
        matcher = StrategyMatcher(db_path=db_path)

        matched_results = []
        for item in selected:
            symbol = item.get("vt_symbol", item.get("code", ""))
            market = "HK" if ".SEHK" in symbol or symbol.startswith("HK.") else "US"
            best_strategy = matcher.select_strategy(symbol, market)
            matched_results.append({
                "symbol": symbol,
                "strategy": best_strategy,
                "score": item.get("score", 0),
            })
            log.info(f"🎯 {symbol} → {best_strategy} score={item.get('score', 0)}")
    except Exception as e:
        log.warning(f"⚠️ 选股/策略匹配失败(非致命): {e}")

    # 10. 启动策略引擎
    if strategy_engine:
        try:
            boot_result = strategy_engine.boot(operator="system")
            log.info(f"🚀 策略启动结果: {boot_result}")
        except Exception as e:
            log.warning(f"⚠️ 策略启动失败: {e}")

    # ===== 11. 启动 DualLink 双链路健康检查（v2.8.3 恢复）=====
    duallink = None
    try:
        duallink = DualLink(main_us=me_us, main_hk=me_hk, db=db)
        duallink.start()
        log.info("✅ DualLink 双链路健康检查已启动")
    except Exception as e:
        log.warning(f"⚠️ DualLink 初始化失败(非致命): {e}")

    # ===== 12. Telegram 远程控制 =====
    token = CONFIG.get("telegram_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")

    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id, machine_registry=registry)
        if notifier.send_message("🚀 Apollo v2.8.3 双引擎版启动完成"):
            log.info("✅ Telegram Bot连接成功")

        controller = RemoteController(db=db, notifier=notifier, config=CONFIG)
        # v2.8.3: 注入双引擎
        controller.set_main_engines(me_us, me_hk)
        if strategy_engine:
            controller.set_strategy_engine(strategy_engine)
        controller.set_registry(registry)

        listener = TelegramCommandListener(
            token, chat_id, controller,
            poll_interval=CONFIG.get("telegram_poll_interval", 3.0)
        )
        listener.start()
        log.info("✅ Telegram 远程控制已启动")

    # ===== 13. 启动 SchedulerJobs 定时调度（v2.8.3 恢复）=====
    scheduler = None
    try:
        from core.scheduler_jobs import SchedulerJobs
        scheduler = SchedulerJobs(config=CONFIG)
        if hasattr(scheduler, "start"):
            scheduler.start()
        log.info("✅ SchedulerJobs 定时调度已启动")
    except ImportError:
        log.info("ℹ️ SchedulerJobs 不可用，跳过")
    except Exception as e:
        log.warning(f"⚠️ SchedulerJobs 启动失败(非致命): {e}")

    # ===== 14. 定时任务线程 =====
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
            # v2.8.3: DualLink 健康检查
            if duallink and hasattr(duallink, "health"):
                try:
                    health = duallink.health()
                    log.info(f"[DualLink] {health}")
                except Exception:
                    pass
            # v2.8.3: 双链路断开自动重连
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

    # 15. 主循环
    log.info(f"🚀 启动完成 | 标识: {registry.summary()} | 模式: 双引擎")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("👋 手动中断 (Ctrl+C)...")
    finally:
        # ===== 安全关闭（v2.8.3 双引擎安全停止）=====
        log.info("🔄 开始安全关闭...")
        if strategy_engine:
            try:
                strategy_engine.stop_all()
            except Exception:
                pass
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
        # 停止双事件引擎
        try:
            ev_us.stop()
        except Exception:
            pass
        try:
            ev_hk.stop()
        except Exception:
            pass
        log.info("✅ 已安全关闭（双引擎已停止）")


if __name__ == "__main__":
    main()
