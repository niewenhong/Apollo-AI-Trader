"""
core/engine.py - Apollo Trader v2.6.0
策略引擎：从数据库读取执行池，动态注册策略到双引擎
"""
import json
import time
from typing import Dict, List, Optional
from vnpy.trader.engine import MainEngine, EventEngine
from vnpy.trader.constant import Interval, Direction, Offset
from vnpy_ctastrategy import CtaTemplate
from core.db_manager import CustomDBManager
from ai.param_advisor import ParamAdvisor


class StrategyEngine:
    """策略注册与管理引擎"""

    def __init__(self, main_us: MainEngine, main_hk: MainEngine, db: CustomDBManager):
        self.main_us = main_us
        self.main_hk = main_hk
        self.db = db
        self.advisor = ParamAdvisor(db)
        self.active_strategies: Dict[str, CtaTemplate] = {}

    def load_strategies(self, market: str = None):
        """从执行池加载策略到对应的引擎"""
        pool = self.db.get_pool(market=market)
        for item in pool:
            vt_symbol = item["vt_symbol"]
            strategy_class = item["strategy_class"]
            params_json = item.get("params_json", "{}")
            params = json.loads(params_json) if params_json else {}

            # 确定目标引擎
            if ".SMART" in vt_symbol:
                engine = self.main_us.cta_engine
            elif ".SEHK" in vt_symbol:
                engine = self.main_hk.cta_engine
            else:
                print(f"[Engine] 未知市场: {vt_symbol}，跳过")
                continue

            # 检查策略是否已存在
            if vt_symbol in self.active_strategies:
                print(f"[Engine] {vt_symbol} 策略已存在，跳过")
                continue

            # 从参数建议获取优化参数
            suggested = self.advisor.suggest(vt_symbol, strategy_class, params)
            if suggested:
                params.update(suggested)

            # 注册策略
            try:
                strategy_name = f"{strategy_class}_{vt_symbol.replace('.','_')}"
                engine.add_strategy(
                    class_name=strategy_class,
                    strategy_name=strategy_name,
                    vt_symbol=vt_symbol,
                    setting=params
                )
                self.active_strategies[vt_symbol] = strategy_name
                print(f"[Engine] 注册策略成功: {strategy_name}")
            except Exception as e:
                print(f"[Engine] 注册策略失败 {vt_symbol}: {e}")

    def start_all(self):
        """启动所有已注册的策略"""
        for engine in [self.main_us.cta_engine, self.main_hk.cta_engine]:
            for name in engine.strategies.keys():
                try:
                    engine.start_strategy(name)
                    print(f"[Engine] 启动策略: {name}")
                except Exception as e:
                    print(f"[Engine] 启动失败 {name}: {e}")

    def stop_all(self):
        """停止所有策略"""
        for engine in [self.main_us.cta_engine, self.main_hk.cta_engine]:
            for name in engine.strategies.keys():
                try:
                    engine.stop_strategy(name)
                except:
                    pass

    def reload_strategies(self):
        """重新加载策略（停掉旧的，加载新的）"""
        self.stop_all()
        self.active_strategies.clear()
        self.load_strategies()
        self.start_all()

    def add_to_pool_and_load(self, vt_symbol: str, market: str,
                              strategy_class: str, params: dict = None):
        """添加到执行池并立即加载"""
        self.db.add_to_pool(vt_symbol, market, strategy_class, params)
        self.load_strategies(market=market)