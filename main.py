"""
main.py — Apollo AI Trader v2.7.0 入口
集成: SubscriptionManager + MarketDataBus + MultiPeriodDB + StrategyMatcher
"""

import time
import json
import logging
import threading
from datetime import datetime

# ===== 日志 =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/main.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("Main")

# ===== 导入 =====
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import Interval

from core.subscription_manager import SubscriptionManager
from core.multi_period_db import MultiPeriodDB
from core.market_data_bus import MarketDataBus
from core.strategy_matcher import StrategyMatcher
from vnpy_futu import FutuGateway

# ===== 加载配置 =====
try:
    with open("config/system_config.json", "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    logger.warning("config/system_config.json 不存在，使用空配置")
    CONFIG = {}

# ===== 初始化引擎 =====
event_engine = EventEngine()
main_engine = MainEngine(event_engine)
main_engine.add_gateway(FutuGateway)
logger.info("✅ MainEngine + EventEngine 初始化完成")

# ===== 数据库 =====
db = MultiPeriodDB("data/history.db")

# ===== 行情总线 =====
market_bus = MarketDataBus(db=db, gate_threshold=3.0)

# ===== 订阅管理器 =====
# 注意：main_engine 单实例，US/HK 通过 symbol 前缀路由
sub_manager = SubscriptionManager(
    main_us=main_engine,
    main_hk=main_engine,
    max_quota=300,
)
sub_manager.db = db
market_bus.strategy_engine = sub_manager  # 复用引用

# ===== 策略匹配器 =====
matcher = StrategyMatcher(db=db)

# ===== 连接网关 =====
futu_cfg = CONFIG.get("futu", {})
us_setting = futu_cfg.get("US", {})
hk_setting = futu_cfg.get("HK", {})

try:
    main_engine.connect(us_setting, "FUTU")
    logger.info("✅ FUTU 网关连接（US配置）")
except Exception as e:
    logger.error(f"US连接失败: {e}")

# 注册K线回调（需在connect之后）
try:
    from vnpy_futu.multi_period_kline_handler import MultiPeriodKlineHandler
    gateway = main_engine.get_gateway("FUTU")
    if gateway:
        handler = MultiPeriodKlineHandler(gateway, market_bus=market_bus)
        gateway.quote_ctx.set_handler(handler)
        logger.info("✅ MultiPeriodKlineHandler 已注册")
except Exception as e:
    logger.error(f"K线Handler注册失败: {e}")

# 行情总线挂载到事件引擎
market_bus.attach_to_engine(event_engine)

# ===== AI选股 → 全套订阅 → 策略匹配 =====
def run_selector_and_subscribe():
    try:
        from ai.stock_selector import AIStockSelector
        selector = AIStockSelector(CONFIG)
        selected = selector.select()
        logger.info(f"📋 选股结果: {selected}")

        for sym in selected:
            sub_manager.subscribe_all(sym)

        for sym in selected:
            sub_manager.get_daily_bars(sym, "2024-01-01",
                                       datetime.now().strftime("%Y-%m-%d"))

        matched = matcher.match(selected)
        for combo in matched:
            logger.info(f"🎯 {combo['symbol']} → {combo['strategy']} "
                        f"params={combo['params']} score={combo['score']}")
        return matched
    except Exception as e:
        logger.error(f"选股流程异常: {e}")
        return []

matched = run_selector_and_subscribe()

# ===== 定时任务 =====
def periodic_task():
    while True:
        time.sleep(300)
        sub_manager.process_unsub_queue()
        sub_manager.audit_quota()

t = threading.Thread(target=periodic_task, daemon=True)
t.start()
logger.info("⏰ 定时任务已启动（5分钟/次）")

# ===== 主循环 =====
logger.info("🚀 Apollo AI Trader v2.7.0 启动完成")
sub_manager.audit_quota()

try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    logger.info("👋 正在关闭...")
    sub_manager.process_unsub_queue()
    sub_manager.audit_quota()
    event_engine.stop()
    logger.info("✅ 已安全关闭")
