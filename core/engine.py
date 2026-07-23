"""
core/engine.py - v2.6.0
策略引擎：管理策略的加载、启动、停止
"""
import logging
from typing import Optional
from vnpy.trader.engine import MainEngine
from core.db_manager import CustomDBManager

logger = logging.getLogger(__name__)


class StrategyEngine:
    """策略引擎"""

    def __init__(self, main_us: MainEngine, main_hk: MainEngine, db: CustomDBManager):
        self.main_us = main_us
        self.main_hk = main_hk
        self.db = db

    def load_strategies(self):
        """加载所有策略配置"""
        # 从数据库或配置文件加载策略列表
        strategies_config = self._load_config()
        for config in strategies_config:
            engine = self._get_cta_engine(config["market"])
            if engine is None:
                logger.warning(f"无法获取 {config['market']} 市场的CTA引擎")
                continue
            try:
                engine.add_strategy(
                    config["class_name"],
                    config["strategy_name"],
                    config["vt_symbol"],
                    config["setting"]
                )
                logger.info(f"策略加载成功: {config['strategy_name']}")
            except Exception as e:
                logger.error(f"策略加载失败: {config['strategy_name']}: {e}")

    def start_all(self):
        """启动所有已加载的策略"""
        # 通过 get_engine 获取 CTA 引擎实例（兼容 vnpy 4.x）
        us_cta = self.main_us.get_engine("CtaStrategy")
        hk_cta = self.main_hk.get_engine("CtaStrategy")
        engines = [eng for eng in [us_cta, hk_cta] if eng is not None]

        for engine in engines:
            if hasattr(engine, 'strategies'):
                for strategy_name in list(engine.strategies.keys()):
                    try:
                        engine.start_strategy(strategy_name)
                        self.db.log_event(f"策略已启动: {strategy_name}")
                        logger.info(f"策略已启动: {strategy_name}")
                    except Exception as e:
                        logger.error(f"启动策略失败 {strategy_name}: {e}")
            else:
                logger.warning("CTA引擎没有 strategies 属性，可能未正确初始化")

        logger.info("所有策略启动完成")

    def stop_all(self):
        """停止所有策略"""
        us_cta = self.main_us.get_engine("CtaStrategy")
        hk_cta = self.main_hk.get_engine("CtaStrategy")
        engines = [eng for eng in [us_cta, hk_cta] if eng is not None]

        for engine in engines:
            if hasattr(engine, 'strategies'):
                for strategy_name in list(engine.strategies.keys()):
                    try:
                        engine.stop_strategy(strategy_name)
                        self.db.log_event(f"策略已停止: {strategy_name}")
                        logger.info(f"策略已停止: {strategy_name}")
                    except Exception as e:
                        logger.error(f"停止策略失败 {strategy_name}: {e}")

    def _get_cta_engine(self, market: str):
        """根据市场获取对应的CTA引擎"""
        main = self.main_us if market == "US" else self.main_hk
        return main.get_engine("CtaStrategy")

    def _load_config(self) -> list:
        """从数据库或配置文件加载策略配置（示例）"""
        # TODO: 实际应从数据库读取
        return [
            {
                "market": "US",
                "class_name": "SellPutStrategy",
                "strategy_name": "SellPut_NVDA",
                "vt_symbol": "NVDA.SMART",
                "setting": {
                    "strike_percent": 0.95,
                    "expiry_days": 30,
                    "premium_target": 0.02,
                    "fixed_size": 1
                }
            },
            {
                "market": "US",
                "class_name": "CoveredCallStrategy",
                "strategy_name": "CoveredCall_AAPL",
                "vt_symbol": "AAPL.SMART",
                "setting": {
                    "call_strike_percent": 1.05,
                    "expiry_days": 30,
                    "premium_target": 0.015,
                    "fixed_size": 100
                }
            }
        ]

    def write_log(self, msg: str):
        """写入日志"""
        logger.info(msg)