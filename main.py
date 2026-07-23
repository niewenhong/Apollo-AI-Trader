"""
main.py - Apollo AI Trader v2.6.0
"""
import sys
import json
import threading
import os
import signal
import time
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from vnpy.trader.engine import MainEngine
from vnpy_futu import FutuGateway

from core.duallink import DualLink
from core.db_manager import CustomDBManager
from core.decision_engine import DecisionEngine
from core.strategy_engine import StrategyEngine
from ai.stock_selector import AIStockSelector
from ai.param_advisor import ParamAdvisor
from ai.llm_client import LLMClient
from ai.report_generator import ReportGenerator
from backtest.optimizer import ParameterOptimizer
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.telegram_webhook import TelegramCommandListener


def main():
    print("=" * 52)
    print("  Apollo AI Trader v2.6.0 启动中...")
    print("=" * 52)

    cfg_path = project_root / "config" / "system_config.json"
    if not cfg_path.exists():
        print(f"[Main] ❌ 缺少 {cfg_path}")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    db_file = project_root / "data" / "apollo_trader.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = CustomDBManager(db_path=str(db_file))

    # 美股
    print("[Main] 启动美股引擎...")
    main_us = MainEngine()
    main_us.add_gateway(FutuGateway)
    main_us.connect(
        {
            "地址": cfg.get("futu.US.address", "127.0.0.1"),
            "端口": cfg.get("futu.US.port", 11111),
            "密码": cfg.get("futu.US.password", ""),
            "市场": cfg.get("futu.US.market", "US"),
            "环境": cfg.get("futu.US.trade_env", "SIMULATE")
        },
        gateway_name="FUTU"
    )

    # 港股
    print("[Main] 启动港股引擎...")
    main_hk = MainEngine()
    main_hk.add_gateway(FutuGateway)
    main_hk.connect(
        {
            "地址": cfg.get("futu.HK.address", "127.0.0.1"),
            "端口": cfg.get("futu.HK.port", 11111),
            "密码": cfg.get("futu.HK.password", ""),
            "市场": cfg.get("futu.HK.market", "HK"),
            "环境": cfg.get("futu.HK.trade_env", "SIMULATE")
        },
        gateway_name="FUTU"
    )

    duallink = DualLink(main_us, main_hk, db)
    duallink.start()

    llm = LLMClient(api_key=cfg.get("llm_api_key", ""))
    decision_engine = DecisionEngine(db=db, llm=llm)
    advisor = ParamAdvisor(db, llm)
    optimizer = ParameterOptimizer(db)

    selector = AIStockSelector(db, main_us, main_hk)

    strategy_engine = StrategyEngine(main_us, main_hk, db)
    strategy_engine.load_strategies(str(project_root / "config" / "strategies.json"))
    strategy_engine.start_all()

    reporter = ReportGenerator(db_manager=db)

    notifier = TelegramNotifier(
        token=cfg.get("telegram_token", ""),
        chat_id=cfg.get("telegram_chat_id", ""),
        db=db, selector=selector, reporter=reporter
    )
    notifier.set_engines(main_us, main_hk)
    notifier.start_polling()

    webhook = TelegramCommandListener(
        notifier=notifier,
        rc=None,
        config={
            "telegram_token": cfg.get("telegram_token", ""),
            "telegram_chat_id": cfg.get("telegram_chat_id", 0),
            "admin_chat_id": cfg.get("telegram_chat_id", 0)
        }
    )

    # ---- 信号处理 ----
    def handle_exit(signum, frame):
        print("\n[Main] 收到退出信号，关闭引擎...")
        strategy_engine.stop_all()
        main_us.close()
        main_hk.close()
        db.close()
        webhook.stop()
        notifier.stop()
        print("[Main] 已安全关闭")
        os._exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print(f"[Main] ✅ 启动完成 {datetime.now():%H:%M:%S}  按 Ctrl+C 退出")
    try:
        # 使用 sleep 循环代替 Event().wait()，确保能被 Ctrl+C 中断
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_exit(None, None)


if __name__ == "__main__":
    main()