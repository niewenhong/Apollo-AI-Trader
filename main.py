"""
main.py — Apollo AI Trader v2.8.1 主入口（含选股+策略匹配+远程控制修复）
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
from vnpy_ctastrategy import CtaStrategyApp

from core.db_manager import DBManager
from core.strategy_engine import StrategyEngine
from core.duallink import DualLink
from core.machine_registry import MachineRegistry
from core.subscription_manager import SubscriptionManager
from core.subscription_plan import apply_subscription_plan
from core.remote_controller import RemoteController
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener
from ai.stock_selector import AIStockSelector
from core.strategy_matcher import StrategyMatcher

# ═══════════════════════════════════════
#  日志
# ═══════════════════════════════════════
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

# ═══════════════════════════════════════
#  配置
# ═══════════════════════════════════════
with open("config/system_config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

log.info(f"🖥️ 本机标识: [{CONFIG.get('opend_host','127.0.0.1')}:{CONFIG.get('opend_port',11111)}]")


def main():
    # 1. 初始化机器注册
    registry = MachineRegistry(CONFIG.get("cluster", {}))
    log.info(f"🏷️ 机器注册: {registry.summary()}")

    # 2. 数据服务
    SETTINGS["datafeed.name"] = "localdata"
    SETTINGS["datafeed.username"] = ""
    SETTINGS["datafeed.password"] = ""
    log.info("📊 数据服务: 本地数据库 (vnpy_localdata)")

    # 3. 主引擎 + CTA
    ev = EventEngine()
    me = MainEngine(ev)
    me.add_app(CtaStrategyApp)
    log.info("✅ CTA 策略应用已加载")

    # 4. 连接双网关
    host = CONFIG["opend_host"]
    port = CONFIG["opend_port"]
    env = CONFIG["trade_env"]

    gw_us = FutuGateway(ev, "FUTU_US")
    gw_hk = FutuGateway(ev, "FUTU_HK")

    # ★★★ 关键修复：手动注册网关到 MainEngine ★★★
    me.gateways["FUTU_US"] = gw_us
    me.gateways["FUTU_HK"] = gw_hk

    gw_us.connect({"地址": host, "端口": port, "市场": "US", "环境": env})
    gw_hk.connect({"地址": host, "端口": port, "市场": "HK", "环境": env})
    log.info("✅ US/HK 网关连接中...")

    timeout = CONFIG.get("connection_timeout", 10)
    for i in range(timeout):
        if gw_us.quote_ctx and gw_hk.quote_ctx:
            log.info("✅ US 行情就绪")
            log.info("✅ HK 行情就绪")
            break
        time.sleep(1)
    else:
        log.error("❌ 行情连接超时")
        sys.exit(1)

    # 5. 订阅管理器 + 订阅计划
    sub_manager = SubscriptionManager(
        max_quota=CONFIG.get("subscription", {}).get("max_quota", 300)
    )
    sub_manager.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)
    apply_subscription_plan(sub_manager, CONFIG)
    sub_manager.audit_quota()

    # 6. DBManager
    db_path = CONFIG.get("db_path") or "data/history.db"
    db = DBManager(db_path=db_path)
    db.ensure_super_user(CONFIG)
    log.info("✅ 数据库管理器就绪")

    # 7. 选股 + 策略匹配
    log.info("🔍 开始AI选股...")
    selector = AIStockSelector(quote_ctx=gw_us.quote_ctx, db=db, market="US")
    selected = selector.select()

    import json as _json
    sample = _json.dumps(selected[:3], ensure_ascii=False, indent=2)[:500]
    log.info(f"📋 选股结果样本: {sample}")

    log.info("🎯 开始策略匹配...")
    matcher = StrategyMatcher(db_path=db_path)

    for item in selected:
        code = item.get("code", "")
        symbol = code.replace("US.", "").replace("HK.", "")
        if not symbol:
            continue
        market = "HK" if code.startswith("HK.") else "US"
        strategy_name = matcher.select_strategy(symbol, market)
        log.info(f"  {symbol} → {strategy_name}")

        db.save_strategy(
            strategy_name=f"{strategy_name}_{symbol}",
            class_name=strategy_name,
            vt_symbol=symbol,
            market=market,
            params={},
            source="matcher",
            modifier="system:strategy_match"
        )

    # 8. 策略引擎
    log.info("🚀 启动策略引擎...")
    strategy_engine = StrategyEngine(
        main_us=me, main_hk=me,
        db=db, config=CONFIG
    )
    boot_result = strategy_engine.boot(operator="system")
    log.info(f"  启动结果: {boot_result}")

    # 9. DualLink
    notifier = None
    token = CONFIG.get("telegram_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")
    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id, machine_registry=registry)

    cta_engine = me.get_engine("CtaStrategy")
    duallink = DualLink(me, cta_engine, notifier)
    duallink.start()

    # 10. 热加载
    strategy_engine.start_hot_reload(
        interval=CONFIG.get("prelive_gate", {}).get("hot_reload_interval", 60)
    )

    # 11. Telegram 远程控制（★ 修复：注入所有引擎到 RemoteController）
    if token and chat_id:
        try:
            notifier.send_message("🚀 Apollo v2.8.1 启动完成")
            log.info("✅ Telegram Bot连接成功")

            # ★★★ 创建 RemoteController 并注入所有依赖 ★★★
            controller = RemoteController(
                db=db,
                notifier=notifier,
                config=CONFIG
            )
            controller.set_main_engine(me)               # 注入 MainEngine
            controller.set_strategy_engine(strategy_engine)  # 注入 StrategyEngine
            controller.set_registry(registry)             # 注入 MachineRegistry

            # ★★★ 将 controller 传给 TelegramCommandListener ★★★
            listener = TelegramCommandListener(
                token,
                chat_id,
                controller=controller,          # ★★★ 关键：传入 controller，而不是 notifier
                poll_interval=CONFIG.get("telegram_poll_interval", 3.0)
            )
            listener.start()
            log.info("✅ Telegram 远程控制已启动")
        except Exception as e:
            log.error(f"Telegram 启动失败: {e}")

    # 12. 定时任务
    def periodic():
        while True:
            time.sleep(60)
            sub_manager.process_unsub_queue()
            registry.heartbeat()
            if int(time.time()) % 600 < 60:
                sub_manager.audit_quota()
                log.info(f"💓 心跳 | {time.strftime('%H:%M:%S')}")

    threading.Thread(target=periodic, daemon=True).start()

    # 13. 主循环
    log.info("🚀 启动完成")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("👋 中断...")
    finally:
        duallink.stop()
        strategy_engine.stop_all()
        ev.stop()
        db.close()
        log.info("✅ 已安全关闭")


if __name__ == "__main__":
    main()