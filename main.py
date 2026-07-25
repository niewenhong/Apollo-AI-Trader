#!/usr/bin/env python3
import sys, json, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from vnpy.trader.engine import MainEngine
from vnpy.trader.setting import SETTINGS
from vnpy_futu import FutuGateway
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp

SETTINGS["log.level"] = "DEBUG"
SETTINGS["datafeed.name"] = "local"

from core.db_manager import CustomDBManager
from core.strategy_engine import StrategyEngine
from core.decision_engine import DecisionEngine
from ai.stock_selector import AIStockSelector           # 正确导入
from ai.stock_diagnosis import StockDiagnosis
from ai.param_advisor import ParamAdvisor
from ai.report_generator import ReportGenerator
from ai.llm_client import LLMClient
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener  # 匹配类名
from monitoring.webhook_server import WebhookServer
from remote.remote_controller import RemoteController
from backtest.optimizer import ParameterOptimizer


def get_quote_ctx(main_engine, retries=10, delay=1):
    for _ in range(retries):
        gateway = main_engine.get_gateway("FUTU")
        if gateway and getattr(gateway, "quote_ctx", None):
            return gateway.quote_ctx
        time.sleep(delay)
    print("[Main] 警告：无法获取 quote_ctx，AI选股降级运行")
    return None


def main():
    print("=" * 48)
    print("  Apollo AI Trader v2.6.0 启动中...")
    print("=" * 48)

    config_path = Path("config/system_config.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    opend_host = cfg.get("opend_host", "127.0.0.1")
    opend_port = cfg.get("opend_port", 11111)
    account = cfg.get("futu_account", "")
    password = cfg.get("futu_password", "")
    env = cfg.get("trade_env", "SIMULATE")

    us_setting = {"地址": opend_host, "端口": opend_port, "市场": "US",
                  "证券账号": account, "密码": password, "环境": env}
    hk_setting = {"地址": opend_host, "端口": opend_port, "市场": "HK",
                  "证券账号": account, "密码": password, "环境": env}

    print("[Main] 启动美股引擎...")
    main_us = MainEngine()
    main_us.add_gateway(FutuGateway)
    main_us.add_app(CtaStrategyApp)
    main_us.add_app(CtaBacktesterApp)
    main_us.connect(us_setting, "FUTU")

    print("[Main] 启动港股引擎...")
    main_hk = MainEngine()
    main_hk.add_gateway(FutuGateway)
    main_hk.add_app(CtaStrategyApp)
    main_hk.add_app(CtaBacktesterApp)
    main_hk.connect(hk_setting, "FUTU")

    time.sleep(3)

    db = CustomDBManager()
    quote_ctx = get_quote_ctx(main_us)
    llm = LLMClient(api_key=cfg.get("llm_api_key", ""))
    selector = AIStockSelector(quote_ctx, db, top_n=cfg.get("ai_top_n", 25), market="US")
    diagnoser = StockDiagnosis(quote_ctx, db)
    advisor = ParamAdvisor(db, llm)
    reporter = ReportGenerator(db)
    optimizer = ParameterOptimizer(db, n_jobs=cfg.get("optimizer_jobs", 4))
    decision_engine = DecisionEngine(db, llm)

    print("[Main] 执行AI选股...")
    try:
        selected = selector.select()
        print(f"[Main] 选股完成: {len(selected)} 只")
    except Exception as e:
        print(f"[Main] 选股失败: {e}")

    strategy_engine = StrategyEngine(main_us, main_hk, db)
    strategy_engine.start_all()

    # ── Telegram 初始化 ──
    tg_token = cfg.get("telegram_token", "")
    tg_chat_id = cfg.get("telegram_chat_id", "")
    proxy = cfg.get("proxy", "")

    notifier = TelegramNotifier(tg_token, tg_chat_id, proxy if proxy else None)
    remote_controller = RemoteController(db, strategy_engine, notifier)

    # 发送启动通知
    notifier.send_message("Apollo AI Trader v2.6.0 已启动")

    # 启动 Telegram 命令轮询
    if tg_token and tg_chat_id:
        listener = TelegramCommandListener(tg_token, tg_chat_id, remote_controller)
        listener.start()
    else:
        print("[Main] 警告：Telegram token/chat_id 未配置，跳过命令轮询")

    # 启动 Webhook 服务器
    webhook = WebhookServer(host="0.0.0.0", port=cfg.get("webhook_port", 8899))
    webhook.register_handler(lambda data: {"status": "ok", "time": datetime.now().isoformat()})
    webhook.start()

    print("[Main] 系统启动完成，进入主循环...")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("[Main] 收到退出信号，关闭引擎...")
        strategy_engine.stop_all()
        main_us.close()
        main_hk.close()
        try:
            db.close()
        except AttributeError:
            pass
        webhook.stop()
        print("[Main] 系统已安全关闭")


if __name__ == "__main__":
    main()