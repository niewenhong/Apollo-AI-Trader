"""
main.py — Apollo AI Trader v2.8.4 最终修正版 + KlineProvider 集成
基于原始 v2.8.3 双引擎架构 + 数据库驱动流水线

关键事实（来自原始代码分析）：
  - MachineRegistry() 无参构造，自动注册+心跳，summary() 返回字符串
  - FutuGateway(event_engine, gateway_name) 两个位置参数
  - StrategyEngine(main_us, main_hk, db, config) 四个位置参数
  - DBManager(db_path) 一个位置参数
  - 流水线：选股→诊股→regime→策略生成写库→引擎从DB加载

修正点：
  1. MachineRegistry 无参构造 + summary()
  2. FutuGateway(ev_us, "FUTU_US") 位置参数
  3. StrategyEngine(main_us=me_us, main_hk=me_hk, db=db, config=CONFIG)
  4. 流水线在合约就绪事件之前执行（确保策略先写入DB）
  5. 删除旧数据库后自动重建所有表
  6. 【新增】流水线写库后立即调用 strategy_engine.boot() 部署策略
  7. 【新增】启动热加载线程，支持运行时数据库变更自动部署
  8. 【新增】KlineProvider 统一K线缓存，减少富途请求
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
    # ===== 0. 删除旧数据库（确保表结构干净）=====
    # 如果 history.db 是旧版本（缺列），直接删掉让 DBManager 重建
    db_path = CONFIG.get("db_path") or os.path.join(BASE_DIR, "data", "history.db")
    db_dir = os.path.dirname(db_path) or "."
    for f in [db_path, db_path + "-wal", db_path + "-shm"]:
        if os.path.exists(f):
            try:
                os.remove(f)
                log.info(f"🗑️ 删除旧数据库: {f}")
            except Exception:
                pass
    os.makedirs(db_dir, exist_ok=True)

    # ===== 1. 机器注册（无参构造）=====
    registry = MachineRegistry()
    log.info(f"🏷️ 机器注册: {registry.summary()}")

    # ===== 2. 数据服务 =====
    SETTINGS["datafeed.name"] = "localdata"
    SETTINGS["datafeed.username"] = ""
    SETTINGS["datafeed.password"] = ""
    log.info("📊 数据服务: 本地数据库 (vnpy_localdata)")

    # ===== 3. 双引擎初始化 =====
    ev_us = EventEngine()
    me_us = MainEngine(ev_us)
    from vnpy_ctastrategy import CtaStrategyApp
    me_us.add_app(CtaStrategyApp)
    log.info("✅ US MainEngine 已创建并加载 CTA 策略应用")

    ev_hk = EventEngine()
    me_hk = MainEngine(ev_hk)
    me_hk.add_app(CtaStrategyApp)
    log.info("✅ HK MainEngine 已创建并加载 CTA 策略应用")

    # ===== 4. 连接 Futu 网关（位置参数：event_engine, gateway_name）=====
    host = CONFIG["opend_host"]
    port = CONFIG["opend_port"]
    env = CONFIG["trade_env"]

    gw_us = FutuGateway(ev_us, "FUTU_US")
    me_us.gateways["FUTU_US"] = gw_us

    gw_hk = FutuGateway(ev_hk, "FUTU_HK")
    me_hk.gateways["FUTU_HK"] = gw_hk

    us_setting = {"地址": host, "端口": port, "市场": "US", "环境": env}
    hk_setting = {"地址": host, "端口": port, "市场": "HK", "环境": env}

    gw_us.connect(us_setting)
    log.info("✅ US 网关连接中...")
    gw_hk.connect(hk_setting)
    log.info("✅ HK 网关连接中...")

    timeout = CONFIG.get("connection_timeout", 15)
    for i in range(timeout):
        us_rdy = gw_us.quote_ctx is not None
        hk_rdy = gw_hk.quote_ctx is not None
        if us_rdy and hk_rdy:
            log.info("✅ US 行情就绪")
            log.info("✅ HK 行情就绪")
            break
        time.sleep(1)
    else:
        if gw_us.quote_ctx is None:
            log.error(f"❌ US 行情超时（{timeout}秒内未就绪）")
        if gw_hk.quote_ctx is None:
            log.error(f"❌ HK 行情超时（{timeout}秒内未就绪）")
        ev_us.stop()
        ev_hk.stop()
        sys.exit(1)

    # ===== 5. 订阅管理 =====
    sub_manager = SubscriptionManager(
        max_quota=CONFIG.get("subscription", {}).get("max_quota", 300)
    )
    sub_manager.set_contexts(gw_us.quote_ctx, gw_hk.quote_ctx)
    apply_subscription_plan(sub_manager, CONFIG)
    sub_manager.audit_quota()

    # ===== 6. 数据库（删除后自动重建所有表）=====
    from core.db_manager import DBManager
    db = DBManager(db_path)
    log.info(f"✅ 数据库就绪: {db_path}")

    # ===== 7. RegimeTrainer =====
    regime_trainer = None
    try:
        from core.regime_trainer import RegimeTrainer
        regime_trainer = RegimeTrainer(config=CONFIG.get("regime", {}), db=db)
        if hasattr(regime_trainer, "start"):
            regime_trainer.start()
        log.info("✅ RegimeTrainer 已初始化")
    except ImportError:
        log.info("ℹ️ RegimeTrainer 不可用，跳过")
    except Exception as e:
        log.warning(f"⚠️ RegimeTrainer 初始化失败(非致命): {e}")

    # ===== 8. ★ StrategyEngine（4参数签名，与v2.8.4一致）=====
    from core.strategy_engine import StrategyEngine
    strategy_engine = StrategyEngine(
        main_us=me_us,
        main_hk=me_hk,
        db=db,
        config=CONFIG,
    )
    log.info("✅ StrategyEngine 已初始化（双引擎模式）")
    # 注意：StrategyEngine 内部已注册 eContractReady 事件，
    # 合约就绪后会自动从 strategy_config 表加载并部署策略。
    # 因此流水线必须在合约就绪事件触发前完成。

    # ===== 8.5 ★ 创建 KlineProvider（统一K线缓存）=====
    from core.kline_provider import KlineProvider
    kp_us = KlineProvider(quote_ctx=gw_us.quote_ctx, market="US")
    # 如果港股也需要选股，可以创建 kp_hk，这里暂不创建
    log.info("✅ KlineProvider(US) 已初始化")

    # ===== 9. ★ 流水线（选股→诊股→regime→写库）=====
    # 关键时序：流水线先写库 → 合约就绪事件后 → 引擎从DB读 → 部署
    log.info("[Pipeline] ═══ 开始执行策略生成流水线 ═══")
    selected = []
    try:
        from ai.stock_selector import AIStockSelector
        # 【修改】传入 kline_provider
        selector = AIStockSelector(
            quote_ctx=gw_us.quote_ctx,
            db=db,
            market="US",
            kline_provider=kp_us  # 新增
        )
        selected = selector.select()
        log.info(f"[Pipeline] ✅ 选股完成: {len(selected)} 只")
    except Exception as e:
        log.warning(f"⚠️ 选股失败(非致命): {e}")

    if selected:
        # --- 9a. 诊股 ---
        try:
            from ai.stock_diagnosis import StockDiagnosis
            # 【修改】传入 kline_provider
            diag = StockDiagnosis(
                quote_ctx=gw_us.quote_ctx,
                db=db,
                kline_provider=kp_us  # 新增
            )
            for item in selected:
                symbol = item.get("vt_symbol", item.get("code", ""))
                if not symbol:
                    continue
                try:
                    # ★★★ 修正：方法名从 run 改为 diagnose ★★★
                    result = diag.diagnose(symbol)
                    # 兼容多种返回值
                    if isinstance(result, (tuple, list)):
                        if len(result) >= 2:
                            summary = str(result[1])
                        else:
                            summary = str(result[0])
                    elif isinstance(result, dict):
                        summary = result.get("summary", str(result))
                    else:
                        summary = str(result)
                    log.info(f"[Pipeline] 🩺 {symbol} → {summary}")
                except Exception as e:
                    log.warning(f"[Pipeline] {symbol} 诊股失败: {e}")
        except Exception as e:
            log.warning(f"⚠️ 诊股模块加载失败: {e}")

        # --- 9b. Regime ---
        if regime_trainer:
            # 【修改】传入 kline_provider
            regime_trainer.kp = kp_us  # 直接注入 kp
            for item in selected:
                symbol = item.get("vt_symbol", item.get("code", ""))
                if not symbol:
                    continue
                try:
                    regime_result = regime_trainer.predict(symbol)
                    log.info(f"[Regime] ✅ {symbol} → {regime_result}")
                except Exception as e:
                    log.warning(f"[Regime] {symbol} 失败: {e}")

        # --- 9c. 策略生成并写入 cta_strategy 表 ---
        try:
            from core.strategy_generator import StrategyGenerator
            # ★★★ 修正：参数名改为 quote_ctx 和 db_path，移除 config ★★★
            generator = StrategyGenerator(
                quote_ctx=gw_us.quote_ctx,
                db_path=db_path,           # 注意：不是 db，而是 db_path
                kline_provider=kp_us
            )
            # ★★★ 修正：方法名从 generate_from_selection 改为 generate_from_selector ★★★
            count = generator.generate_from_selector(selected)
            log.info(f"[Pipeline] ✅ 策略生成完成: {count}个写入 cta_strategy 表")
        except Exception as e:
            log.error(f"❌ 策略生成失败: {e}")

        # ===== ★【新增】立即部署流水线生成的策略 =====
        deployed_count = strategy_engine.boot(operator="pipeline")
        log.info(f"[Pipeline] ✅ 立即部署 {deployed_count} 个策略")

        # ===== ★【新增】启动热加载（每60秒检查数据库变更）=====
        if hasattr(strategy_engine, 'start_hot_reload'):
            strategy_engine.start_hot_reload(interval=60)
            log.info("[Pipeline] ✅ 热加载已启动")

    else:
        log.info("[Pipeline] ⚠️ 无选股结果，跳过流水线")
    log.info("[Pipeline] ═══ 流水线执行完毕 ═══")

    # ===== 10. 启动 DualLink =====
    duallink = None
    try:
        duallink = DualLink(main_us=me_us, main_hk=me_hk, db=db)
        duallink.start()
        log.info("✅ DualLink 双链路健康检查已启动")
    except Exception as e:
        log.warning(f"⚠️ DualLink 初始化失败(非致命): {e}")

    # ===== 11. Telegram =====
    token = CONFIG.get("telegram_token", "")
    chat_id = CONFIG.get("telegram_chat_id", "")

    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id, machine_registry=registry)
        try:
            notifier.send_message("🚀 Apollo v2.8.4 启动完成")
            log.info("✅ Telegram Bot连接成功")
        except Exception as e:
            log.warning(f"⚠️ Telegram 发送失败: {e}")

        controller = RemoteController(db=db, notifier=notifier, config=CONFIG)
        controller.set_main_engines(me_us, me_hk)
        controller.set_strategy_engine(strategy_engine)
        controller.set_registry(registry)

        listener = TelegramCommandListener(
            token, chat_id, controller,
            poll_interval=CONFIG.get("telegram_poll_interval", 3.0)
        )
        listener.start()
        log.info("✅ Telegram 远程控制已启动")

    # ===== 12. SchedulerJobs =====
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

    # ===== 13. 后台维护线程 =====
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
                    health = duallink.health()
                    log.info(f"[DualLink] {health}")
                except Exception:
                    pass
            if duallink and hasattr(duallink, "reconnect_if_needed"):
                try:
                    duallink.reconnect_if_needed()
                except Exception:
                    pass
            # 每10分钟审计一次配额
            if int(time.time()) % 600 < 60:
                try:
                    sub_manager.audit_quota()
                except Exception:
                    pass

    threading.Thread(target=periodic, daemon=True).start()

    # ===== 14. 主循环 =====
    log.info(f"🚀 启动完成 | {registry.summary()} | 模式: 双引擎 + 数据库驱动流水线")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("👋 收到中断信号 (Ctrl+C)...")
    finally:
        log.info("🔄 开始安全关闭...")
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
        log.info("✅ 已安全关闭（双引擎已停止）")


if __name__ == "__main__":
    main()