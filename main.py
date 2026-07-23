#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apollo AI Trader v2.6.0
双引擎双链路 + 单OpenD + AI选股/诊股/参数优化 + 数据库驱动策略 + Telegram全功能
"""
import sys
import os
import json
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from vnpy.trader.engine import MainEngine
from vnpy.trader.setting import SETTINGS
from vnpy_futu import FutuGateway
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo
    sys.modules['zoneinfo'] = zoneinfo

# 日志级别
SETTINGS["log.level"] = "debug"

# 禁用数据服务（富途是交易接口，不是数据服务）
SETTINGS["datafeed.name"] = ""

os.makedirs("data/database", exist_ok=True)

# 导入自定义模块
from core.db_manager import CustomDBManager
from core.strategy_engine import StrategyEngine
from core.decision_engine import DecisionEngine
from ai.stock_selector import AIStockSelector
from ai.stock_diagnosis import StockDiagnosis
from ai.param_advisor import ParamAdvisor
from ai.report_generator import ReportGenerator
from ai.llm_client import LLMClient
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.webhook_server import WebhookServer
from backtest.optimizer import ParamOptimizer


def main():
    print("=" * 56)
    print("  Apollo AI Trader v2.6.0 启动中...")
    print("=" * 56)

    # ---- 加载配置 ----
    config_path = Path("config/system_config.json")
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    else:
        cfg = {}
        print("[Main] ⚠️ 配置文件不存在，使用默认值")

    opend_host = cfg.get("opend_host", "127.0.0.1")
    opend_port = cfg.get("opend_port", 11111)
    account = cfg.get("futu_account", "")
    password = cfg.get("futu_password", "")
    env = cfg.get("trade_env", "SIMULATE")

    # ---- 创建双引擎 ----
    us_setting = {
        "地址": opend_host, "端口": opend_port,
        "市场": "US", "证券账号": account,
        "密码": password, "环境": env
    }
    hk_setting = {
        "地址": opend_host, "端口": opend_port,
        "市场": "HK", "证券账号": account,
        "密码": password, "环境": env
    }

    print("[Main] 启动美股引擎...")
    main_us = MainEngine()
    gateway_us = main_us.add_gateway(FutuGateway)
    main_us.add_app(CtaStrategyApp)
    main_us.add_app(CtaBacktesterApp)
    main_us.connect(us_setting, "FUTU")

    print("[Main] 启动港股引擎...")
    main_hk = MainEngine()
    gateway_hk = main_hk.add_gateway(FutuGateway)
    main_hk.add_app(CtaStrategyApp)
    main_hk.add_app(CtaBacktesterApp)
    main_hk.connect(hk_setting, "FUTU")

    time.sleep(3)

    # ---- 初始化数据库 ----
    db = CustomDBManager()

    # ---- 初始化AI模块 ----
    quote_ctx = gateway_us.quote_ctx
    llm = LLMClient(api_key=cfg.get("llm_api_key", ""))
    selector = AIStockSelector(quote_ctx, db, top_n=cfg.get("ai_top_n", 25), market="US")
    diagnoser = StockDiagnosis(quote_ctx, db)
    advisor = ParamAdvisor(db, llm)
    reporter = ReportGenerator(db)
    optimizer = ParamOptimizer(db, n_jobs=4)
    decision_engine = DecisionEngine(db, llm)

    # ---- AI选股 ----
    print("[Main] 执行AI选股...")
    try:
        selected = selector.select()
        print(f"[Main] 选股完成: {len(selected)} 只")
    except Exception as e:
        print(f"[Main] 选股失败: {e}")

    # ---- 策略引擎 ----
    print("[Main] 加载交易策略...")
    strategy_engine = StrategyEngine(main_us, main_hk, db)
    strategy_engine.load_strategies("config/strategies.json")
    strategy_engine.start_all()

    # ---- Telegram通知 ----
    tg_token = cfg.get("telegram_token", "")
    tg_chat_id_raw = cfg.get("telegram_chat_id", "")
    tg_chat_id = str(tg_chat_id_raw) if tg_chat_id_raw is not None else ""

    notifier = TelegramNotifier(tg_token, tg_chat_id, db, selector, diagnoser, reporter)
    notifier.set_engines(main_us, main_hk)
    notifier.start_polling()

    # ---- Webhook服务器 ----
    webhook = WebhookServer(host="0.0.0.0", port=cfg.get("webhook_port", 8899))
    webhook.register_handler(lambda data: {"status": "ok", "time": datetime.now().isoformat()})
    webhook.start()

    # ---- 启动通知 ----
    notifier.send_sync("🚀 Apollo AI Trader v2.6.0 已启动\n交易策略已加载并运行")

    print("[Main] 系统启动完成，进入主循环...")
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[Main] 收到退出信号，关闭引擎...")
        strategy_engine.stop_all()
        main_us.close()
        main_hk.close()
        db.close()
        webhook.stop()
        notifier.stop()
        print("[Main] 系统已安全关闭")


if __name__ == "__main__":
    main()
