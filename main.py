"""
main.py — Apollo AI Trader v2.7.0 启动入口（精简版）
功能：双网关(US 8只 + HK 2只) → 选股 → 全套订阅 → 策略匹配 → Telegram远程控制
"""

import json
import time
import logging
import threading

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy_futu import FutuGateway

from core.multi_period_db import MultiPeriodDB
from core.subscription_manager import SubscriptionManager
from core.strategy_matcher import StrategyMatcher
from core.remote_controller import RemoteController

from ai.stock_selector import AIStockSelector

from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener

# ========== 日志配置 ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/main.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("Main")

# ========== 1. 加载配置 ==========
with open("config/system_config.json", "r", encoding="utf-8") as f:
    C = json.load(f)

log.info(f"🖥️ 本机标识: [{C.get('opend_host','127.0.0.1')}:{C.get('opend_port',11111)}]")

# ========== 2. 初始化引擎 + 双网关 ==========
ev = EventEngine()
me = MainEngine(ev)
me.add_gateway(FutuGateway)

gw_us = FutuGateway(ev, "FUTU_US")
gw_hk = FutuGateway(ev, "FUTU_HK")
me.gateways["FUTU_US"] = gw_us
me.gateways["FUTU_HK"] = gw_hk

# ========== 3. 核心组件 ==========
db = MultiPeriodDB(C.get("db_path") or "data/apollo.db")
sub = SubscriptionManager(max_quota=300)
matcher = StrategyMatcher(db=db)

# ========== 4. 连接网关 ==========
host = C["opend_host"]
port = C["opend_port"]
env = C["trade_env"]

us_setting = {"地址": host, "端口": port, "市场": "US", "环境": env}
hk_setting = {"地址": host, "端口": port, "市场": "HK", "环境": env}

gw_us.connect(us_setting)
log.info("✅ US 网关连接中...")

gw_hk.connect(hk_setting)
log.info("✅ HK 网关连接中...")

# ========== 5. 等待行情就绪 ==========
def wait_ctx(gw, name, timeout=10):
    for i in range(timeout):
        if gw.quote_ctx is not None:
            log.info(f"✅ {name} 行情就绪")
            return True
        time.sleep(1)
    log.error(f"❌ {name} 行情超时")
    return False

wait_ctx(gw_us, "US")
wait_ctx(gw_hk, "HK")

sub.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)

# ========== 6. 订阅（8美 + 2港）==========
universe = {
    "US": ["US.NVDA", "US.AAPL", "US.MSFT", "US.AMZN",
           "US.META", "US.GOOGL", "US.AMD", "US.TSLA"],
    "HK": ["HK.00700", "HK.09988"]
}

for s in universe["US"] + universe["HK"]:
    sub.subscribe_all(s)

# ========== 7. 选股 + 策略匹配 ==========
selector = AIStockSelector(quote_ctx=gw_us.quote_ctx, db=db, market="US")
selected = selector.select()
for m in matcher.match(selected):
    log.info(f"🎯 {m['symbol']} → {m['strategy']} score={m['score']}")

# ========== 8. Telegram 远程控制 ==========
token = C.get("telegram_token", "")
chat_id = C.get("telegram_chat_id", "")
proxy = C.get("proxy", "") or None

notifier = controller = listener = None

if token and chat_id:
    notifier = TelegramNotifier(token, chat_id, proxy=proxy)
    # 测试连接
    ok = notifier.send_message("🚀 Apollo v2.7.0 启动中...")
    if ok:
        log.info("✅ Telegram Bot连接成功")
    else:
        log.warning("⚠️ Telegram Bot连接失败（检查token/chat_id/网络）")

    tg_config = {"remote_password": C.get("remote_password", "")}
    controller = RemoteController(db=db, notifier=notifier, config=tg_config)
    controller.set_main_engine(me)

    listener = TelegramCommandListener(token, chat_id, controller, poll_interval=3.0)
    listener.start()
    log.info("✅ Telegram 远程控制已启动")
else:
    log.warning("⚠️ Telegram 未配置（缺少 telegram_token 或 telegram_chat_id）")

# ========== 9. 定时任务 ==========
def periodic_task():
    while True:
        time.sleep(300)
        sub.audit_quota()
        # 5分钟心跳
        if notifier:
            notifier.notify("HEARTBEAT", f"运行中 | 订阅{len(sub.subscribed)}只")

threading.Thread(target=periodic_task, daemon=True).start()

# ========== 10. 主循环（带退出检测）==========
log.info(f"🚀 启动完成 已订阅{len(sub.subscribed)}只 (美{len(universe['US'])}+港{len(universe['HK'])})")

try:
    while True:
        time.sleep(1)
        if controller and controller.is_shutdown_requested():
            log.info("👋 收到关闭信号，退出主循环...")
            break
except KeyboardInterrupt:
    log.info("👋 手动中断 (Ctrl+C)...")
finally:
    ev.stop()
    log.info("✅ 已安全关闭")