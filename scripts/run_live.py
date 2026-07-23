# -*- coding: utf-8 -*-
"""
实盘模式入口（无人值守）
- 连接 Futu/IB 真实账户
- 启动所有策略
- 启动心跳监控
- 启动远程控制监听
"""
import sys
import os
import json
import logging
import signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp
from vnpy.gateway.futu import FutuGateway
from vnpy.gateway.ib import IBGateway
from vnpy_ctastrategy import CtaStrategyApp

from core.engine import ApolloEngine
from strategies.equity.vwap_strategy import VwapStrategy
from strategies.equity.triple_filter_scalp_strategy import TripleFilterScalpStrategy
from strategies.futures.momentum_strategy import FuturesMomentumStrategy
from monitoring.heartbeat import HeartbeatMonitor
from monitoring.telegram_notifier import TelegramNotifier
from monitoring.openclaw_client import OpenClawClient
from monitoring.remote_control import RemoteController
from utils.logger import setup_logging

# 全局引用（用于信号处理）
apollo = None
heartbeat = None


def signal_handler(sig, frame):
    """捕获 Ctrl+C / SIGTERM"""
    logger = logging.getLogger("scripts.run_live")
    logger.critical(f"收到信号 {sig}，正在优雅停止...")
    if apollo:
        apollo.stop_all()
    if heartbeat:
        heartbeat.stop()
    sys.exit(0)


def main():
    global apollo, heartbeat

    setup_logging(os.path.join(ROOT, "config/logging_config.json"))
    logger = logging.getLogger("scripts.run_live")
    logger.info("=" * 60)
    logger.info("  Apollo AI Trader v2.2.0 — LIVE MODE")
    logger.info("=" * 60)

    # 注册信号
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 加载全局配置
    config_path = os.path.join(ROOT, "config/accounts_config.json")
    with open(config_path, "r") as f:
        accounts = json.load(f)

    # 创建 Qt 应用
    qapp = create_qapp()

    # 主引擎
    main_engine = MainEngine()
    logger.info("[Live] MainEngine 已创建")

    # 添加网关
    futu_cfg = accounts.get("futu", {})
    if futu_cfg.get("accounts"):
        for acc in futu_cfg["accounts"]:
            if acc.get("enabled"):
                setting = {"host": futu_cfg.get("host", "127.0.0.1"),
                           "port": futu_cfg.get("port", 11111),
                           "market": acc.get("type", "SIMULATE").upper()}
                main_engine.add_gateway(FutuGateway, setting)
                logger.info(f"[Live] Futu 已连接: {acc['name']} ({setting['market']})")

    ib_cfg = accounts.get("ib", {})
    if ib_cfg.get("accounts"):
        for acc in ib_cfg["accounts"]:
            if acc.get("enabled"):
                setting = {"host": ib_cfg.get("host", "127.0.0.1"),
                           "port": ib_cfg.get("port", 7497),
                           "client_id": ib_cfg.get("client_id", 1)}
                main_engine.add_gateway(IBGateway, setting)
                logger.info(f"[Live] IB 已连接: {acc['name']}")

    # CTA 应用
    cta_app = main_engine.add_app(CtaStrategyApp)

    # Apollo 引擎
    apollo = ApolloEngine(main_engine=main_engine)
    apollo.register_strategy_class("VwapStrategy", VwapStrategy)
    apollo.register_strategy_class("TripleFilterScalpStrategy", TripleFilterScalpStrategy)
    apollo.register_strategy_class("FuturesMomentumStrategy", FuturesMomentumStrategy)

    # 根据 symbols_config 自动添加策略
    symbols_path = os.path.join(ROOT, "config/symbols_config.json")
    if os.path.exists(symbols_path):
        with open(symbols_path, "r") as f:
            symbols = json.load(f)
        for sym, info in symbols.get("equity", {}).items():
            if not info.get("enabled"):
                continue
            vt_symbol = f"{sym}.{info.get('exchange', 'SMART')}"
            strategy_type = info.get("suggested_strategy", "vwap")
            class_map = {
                "vwap": "VwapStrategy",
                "triple_filter": "TripleFilterScalpStrategy",
                "trend": "VwapStrategy",  # fallback
            }
            cls_name = class_map.get(strategy_type, "VwapStrategy")
            try:
                apollo.add_strategy(
                    name=f"{strategy_type}_{sym.lower()}",
                    strategy_name=cls_name,
                    vt_symbol=vt_symbol
                )
                logger.info(f"[Live] 添加: {sym} → {cls_name}")
            except Exception as e:
                logger.error(f"[Live] 添加失败 {sym}: {e}")

    # 初始化并启动
    apollo.init_all()
    apollo.start_all()

    # 心跳监控
    notifier = TelegramNotifier()
    heartbeat = HeartbeatMonitor(interval=30, callback=lambda s: notifier.send_message(f"💓 {s}", "info"))
    heartbeat.start()
    logger.info("[Live] 心跳已启动")

    # 远程控制
    remote = RemoteController(engine=apollo)
    openclaw = OpenClawClient()
    logger.info(f"[Live] 远程控制已就绪 (IP: {openclaw.local_ip})")

    # 主窗口
    main_window = MainWindow(main_engine)
    main_window.show_maximized()

    logger.info("[Live] 系统已启动，进入事件循环...")
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
