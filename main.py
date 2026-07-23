"""
main.py - Apollo AI Trader v2.6.0
"""
import sys
import os
import json
import threading
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from vnpy.trader.engine import MainEngine

from core.duallink import DualLink
from core.db_manager import CustomDBManager
from core.decision_engine import DecisionEngine
from core.strategy_engine import StrategyEngine
from ai.stock_selector import StockSelector
from ai.param_advisor import ParamAdvisor
from ai.llm_client import LLMClient
from ai.report_generator import ReportGenerator
from backtest.optimizer import ParameterOptimizer
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramWebhook


def main():
    print("=" * 52)
    print("  Apollo AI Trader v2.6.0 启动中...")
    print("=" * 52)

    cfg_path = project_root / "config" / "system_config.json"
    if not cfg_path.exists():
        print(f"[Main] ❌ 缺少配置文件 {cfg_path}")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # DB
    db_file = project_root / "data" / "apollo_trader.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = CustomDBManager(db_path=str(db_file))

    # 美股引擎
    print("[Main] 启动美股引擎...")
    main_us = MainEngine()
    main_us.add_gateway("FUTU")
    main_us.connect({
        "td_uri": "ws://127.0.0.1:11111/api",
        "td_env": 1,
        "trade_pwd": cfg.get("futu", {}).get("US", {}).get("password", ""),
        "md_uri": "ws://127.0.0.1:11111/api",
        "md_env": 1,
        "market": "US"
    })

    # 港股引擎
    print("[Main] 启动港股引擎...")
    main_hk = MainEngine()
    main_hk.add_gateway("FUTU")
    main_hk.connect({
        "td_uri": "ws://127.0.0.1:11111/api",
        "td_env": 1,
        "trade_pwd": cfg.get("futu", {}).get("HK", {}).get("password", ""),
        "md_uri": "ws://127.0.0.1:11111/api",
        "md_env": 1,
        "market": "HK"
    })

    # 双链路（类名 DualLink）
    duallink = DualLink(main_us, main_hk, db)
    duallink.start()

    # LLM + 决策引擎
    llm = LLMClient(api_key=cfg.get("llm_api_key", ""))
    decision_engine = DecisionEngine(db=db, main_us=main_us, main_hk=main_hk, llm=llm)
    advisor = ParamAdvisor(db, llm)
    optimizer = ParameterOptimizer(db)   # ← 只接 db，不接 decision_engine，不接 n_jobs

    # 策略引擎
    strategy_engine = StrategyEngine(main_us, main_hk, db)
    strategy_engine.load_strategies("config/strategies.json")
    strategy_engine.start_all()

    # 选股 + 日报
    selector = StockSelector(db, main_us, main_hk, llm=llm)
    reporter = ReportGenerator(db=db, selector=selector, main_us=main_us,
                               main_hk=main_hk, decision_engine=decision_engine,
                               optimizer=optimizer)

    # Telegram
    tg_cfg = cfg.get("telegram", {})
    notifier = TelegramNotifier(token=tg_cfg.get("token", ""),
                                chat_id=tg_cfg.get("chat_id", ""),
                                db=db, selector=selector, reporter=reporter)
    notifier.set_engines(main_us, main_hk)

    webhook = TelegramWebhook(port=cfg.get("webhook_port", 8899),
                              token=tg_cfg.get("token", ""),
                              chat_id=tg_cfg.get("chat_id", ""),
                              notifier=notifier)
    webhook.start()
    notifier.start_polling()

    print(f"[Main] ✅ 启动完成 {datetime.now():%H:%M:%S}  Ctrl+C 退出")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\n[Main] 关闭中...")
        strategy_engine.stop_all()
        main_us.close()
        main_hk.close()
        db.close()
        webhook.stop()
        notifier.stop()
        print("[Main] 已安全关闭")


if __name__ == "__main__":
    main()