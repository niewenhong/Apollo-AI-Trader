# -*- coding: utf-8 -*-
"""
调试模式入口
- 单策略、单标的
- 详细日志 + 单步执行
- 连接 Futu 模拟盘
"""
import sys
import os
import logging

# 确保项目根目录在 path 中
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp
from vnpy.gateway.futu import FutuGateway
from vnpy_ctastrategy import CtaStrategyApp

from core.engine import ApolloEngine
from strategies.equity.vwap_strategy import VwapStrategy
from strategies.equity.triple_filter_scalp_strategy import TripleFilterScalpStrategy
from utils.logger import setup_logging

def main():
    # 初始化日志
    setup_logging(os.path.join(ROOT, "config/logging_config.json"))
    logger = logging.getLogger("scripts.run_debug")
    logger.info("=" * 60)
    logger.info("  Apollo AI Trader v2.2.0 — DEBUG MODE")
    logger.info("=" * 60)

    # 创建 Qt 应用
    qapp = create_qapp()

    # 创建主引擎
    main_engine = MainEngine()
    logger.info("[Debug] MainEngine 已创建")

    # 添加富途网关
    futu_setting = {
        "host": "127.0.0.1",
        "port": 11111,
        "market": "SIMULATE"
    }
    main_engine.add_gateway(FutuGateway, futu_setting)
    logger.info("[Debug] FutuGateway 已添加 (SIMULATE)")

    # 添加 CTA 策略应用
    cta_app = main_engine.add_app(CtaStrategyApp)
    logger.info("[Debug] CtaStrategyApp 已添加")

    # 创建 Apollo 引擎
    apollo = ApolloEngine(main_engine=main_engine)
    apollo.register_strategy_class("VwapStrategy", VwapStrategy)
    apollo.register_strategy_class("TripleFilterScalpStrategy", TripleFilterScalpStrategy)
    logger.info("[Debug] 策略类已注册")

    # 添加策略实例（调试用）
    try:
        vwap = apollo.add_strategy(
            name="vwap_nvda",
            strategy_name="VwapStrategy",
            vt_symbol="NVDA.SMART",
            settings={"debug_mode": True, "dry_run": False}
        )
        logger.info(f"[Debug] 策略已添加: vwap_nvda @ NVDA.SMART")
    except Exception as e:
        logger.error(f"[Debug] 添加策略失败: {e}")

    # 初始化并启动
    apollo.init_all()
    apollo.start_all()

    # 创建主窗口
    main_window = MainWindow(main_engine)
    main_window.show_maximized()

    logger.info("[Debug] 系统已启动，进入事件循环...")
    sys.exit(qapp.exec())

if __name__ == "__main__":
    main()
