"""
core/strategy_engine.py - v2.6.0
策略引擎：加载/启动/停止策略，管理下单
"""
import json
import os
import importlib
from datetime import datetime
from vnpy.trader.object import OrderData, TradeData, TickData
from vnpy.trader.constant import Direction, Offset


class StrategyEngine:
    """
    策略引擎管理器
    - 从配置文件加载策略
    - 创建策略实例
    - 管理策略生命周期
    - 统一下单接口
    """

    def __init__(self, main_us=None, main_hk=None, db=None):
        self.main_us = main_us
        self.main_hk = main_hk
        self.db = db
        self.strategies = {}
        self._main_engine_map = {}

    def load_strategies(self, config_path: str = "config/strategies.json"):
        if not os.path.exists(config_path):
            print(f"[StrategyEngine] 策略配置文件不存在: {config_path}")
            return

        with open(config_path, "r", encoding="utf-8") as f:
            configs = json.load(f)

        print(f"[StrategyEngine] 加载 {len(configs)} 个策略配置")

        for cfg in configs:
            market = cfg.get("market", "US")
            class_name = cfg.get("class_name")
            strategy_name = cfg.get("strategy_name")
            vt_symbol = cfg.get("vt_symbol")
            setting = cfg.get("setting", {})

            main_engine = self.main_us if market == "US" else self.main_hk
            self._main_engine_map[strategy_name] = main_engine

            try:
                module_name = class_name.lower()
                if module_name.startswith("sellput"):
                    module_name = "sell_put_strategy"
                elif module_name.startswith("coveredcall"):
                    module_name = "covered_call_strategy"
                module = importlib.import_module(f"strategies.{module_name}")
                strategy_class = getattr(module, class_name)
            except (ImportError, AttributeError) as e:
                print(f"[StrategyEngine] 导入策略失败 {class_name}: {e}")
                continue

            strategy = strategy_class(
                strategy_engine=self,
                strategy_name=strategy_name,
                vt_symbol=vt_symbol,
                setting=setting
            )
            strategy.main_engine = main_engine

            strategy.on_init()
            self.strategies[strategy_name] = strategy
            print(f"[StrategyEngine] 策略已加载: {strategy_name} ({market})")

    def start_all(self):
        for name, strategy in self.strategies.items():
            try:
                strategy.on_start()
                main_engine = strategy.main_engine
                if main_engine:
                    main_engine.subscribe(strategy.vt_symbol, "FUTU")
                    print(f"[StrategyEngine] {name} 已启动，订阅 {strategy.vt_symbol}")
            except Exception as e:
                print(f"[StrategyEngine] 启动策略 {name} 失败: {e}")

    def stop_all(self):
        for name, strategy in self.strategies.items():
            try:
                strategy.on_stop()
                print(f"[StrategyEngine] {name} 已停止")
            except Exception as e:
                print(f"[StrategyEngine] 停止策略 {name} 失败: {e}")

    def send_order(self, strategy, vt_symbol, direction, offset, volume, order_type, price=None):
        main_engine = getattr(strategy, 'main_engine', None)
        if not main_engine:
            print(f"[StrategyEngine] ⚠️ 策略 {strategy.strategy_name} 没有引擎引用")
            return ""

        try:
            order_id = main_engine.send_order(
                symbol=vt_symbol,
                direction=direction,
                offset=offset,
                volume=volume,
                price=price,
                order_type=order_type,
                gateway_name="FUTU"
            )
            log_msg = (
                f"📤 下单: {direction.value} {vt_symbol} "
                f"数量 {volume} 价格 {price or '市价'}"
            )
            print(f"[StrategyEngine] {log_msg}")
            if self.db:
                self.db.log_event(log_msg)
            return order_id
        except Exception as e:
            err_msg = f"❌ 下单失败: {e}"
            print(f"[StrategyEngine] {err_msg}")
            if self.db:
                self.db.log_event(err_msg)
            return ""

    def on_tick(self, vt_symbol, tick):
        for strategy in self.strategies.values():
            if getattr(strategy, 'vt_symbol', '') == vt_symbol:
                strategy.on_tick(tick)

    def on_trade(self, trade):
        for strategy in self.strategies.values():
            if hasattr(strategy, 'on_trade'):
                strategy.on_trade(trade)

    def on_order(self, order):
        for strategy in self.strategies.values():
            if hasattr(strategy, 'on_order'):
                strategy.on_order(order)

    def get_all_strategies(self):
        result = []
        for name, s in self.strategies.items():
            result.append({
                "name": name,
                "pos": getattr(s, 'pos', 0),
                "total_premium": getattr(s, 'total_premium', 0),
                "trades": getattr(s, 'trades_count', 0),
                "last_trade": getattr(s, 'last_trade_time', "")
            })
        return result
