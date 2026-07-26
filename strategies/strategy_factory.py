"""
strategies/strategy_factory.py - v2.8.0
策略工厂：统一注册、创建、管理所有策略类
支持 vnpy CtaStrategy 引擎自动发现
"""
import importlib
import logging
from typing import Dict, Type, Any, Optional

from vnpy_ctastrategy import CtaTemplate

logger = logging.getLogger("StrategyFactory")


class StrategyFactory:
    """
    策略工厂：负责策略类的注册、发现和实例化
    
    使用方式：
        factory = StrategyFactory()
        factory.auto_discover()  # 自动发现 strategies/ 下的所有策略
        cls = factory.get_class("MultiIndicatorStrategy")
        instance = factory.create(cta_engine, "my_strat", "HK.00700", {"ma_fast": 3})
    """

    def __init__(self):
        self._registry: Dict[str, Type[CtaTemplate]] = {}
        self._metadata: Dict[str, dict] = {}

    def register(self, cls: Type[CtaTemplate], metadata: Optional[dict] = None):
        """注册策略类"""
        name = cls.__name__
        if name in self._registry:
            logger.warning(f"[Factory] 策略 {name} 已注册，将被覆盖")
        self._registry[name] = cls
        self._metadata[name] = metadata or {}
        logger.info(f"[Factory] ✅ 注册策略: {name}")

    def get_class(self, class_name: str) -> Optional[Type[CtaTemplate]]:
        """获取策略类"""
        return self._registry.get(class_name)

    def create(self, cta_engine, strategy_name: str, vt_symbol: str,
               setting: dict) -> Optional[CtaTemplate]:
        """创建策略实例"""
        class_name = setting.get("class_name", strategy_name)
        cls = self.get_class(class_name)
        if cls is None:
            logger.error(f"[Factory] 未找到策略类: {class_name}")
            return None
        try:
            instance = cls(cta_engine, strategy_name, vt_symbol, setting)
            return instance
        except Exception as e:
            logger.error(f"[Factory] 创建策略 {strategy_name} 失败: {e}")
            return None

    def auto_discover(self, strategies_dir: str = "strategies"):
        """
        自动发现并注册 strategies/ 目录下所有 CtaTemplate 子类
        """
        import os
        import glob

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = os.path.join(base, strategies_dir, "**", "*.py")
        files = glob.glob(pattern, recursive=True)

        for f in files:
            if f.endswith("__init__.py") or f.endswith("strategy_factory.py"):
                continue
            self._try_import_file(f, base)

    def _try_import_file(self, filepath: str, base: str):
        """尝试导入单个文件并注册其中的策略类"""
        import os
        rel = os.path.relpath(filepath, base)
        module_name = rel.replace(os.sep, ".").replace(".py", "")

        try:
            mod = importlib.import_module(module_name)
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if (isinstance(obj, type) and
                        issubclass(obj, CtaTemplate) and
                        obj is not CtaTemplate):
                    self.register(obj)
        except Exception as e:
            logger.warning(f"[Factory] 导入 {module_name} 失败: {e}")

    def list_strategies(self) -> list:
        """列出所有已注册策略"""
        result = []
        for name, cls in self._registry.items():
            params = list(getattr(cls, 'parameters', []))
            meta = self._metadata.get(name, {})
            result.append({
                "name": name,
                "params": params,
                "description": meta.get("description", ""),
                "author": getattr(cls, 'author', 'Unknown'),
            })
        return result

    def get_default_params(self, class_name: str) -> dict:
        """获取策略默认参数"""
        cls = self.get_class(class_name)
        if cls is None:
            return {}
        params = getattr(cls, 'parameters', [])
        # 尝试从类属性获取默认值
        defaults = {}
        for p in params:
            defaults[p] = getattr(cls, p, None)
        return defaults


# ========== 全局工厂实例 ==========
factory = StrategyFactory()

# 启动时自动注册内置策略
try:
    from strategies.equity.multi_indicator_strategy import MultiIndicatorStrategy
    factory.register(MultiIndicatorStrategy, {
        "description": "10维共振多指标策略（均线+RSI+MACD+布林带+ATR+成交量）",
        "market": ["US", "HK"],
        "type": "equity",
    })
except ImportError:
    pass

try:
    from strategies.equity.order_flow_strategy import TickOrderFlowStrategy
    factory.register(TickOrderFlowStrategy, {
        "description": "Tick级订单流策略（买卖盘失衡+Kelly仓位+移动止盈）",
        "market": ["US", "HK"],
        "type": "equity",
    })
except ImportError:
    pass

try:
    from strategies.equity.grid_strategy import GridStrategy
    factory.register(GridStrategy, {
        "description": "网格交易策略（震荡市高抛低吸）",
        "market": ["US", "HK"],
        "type": "equity",
    })
except ImportError:
    pass

try:
    from strategies.equity.trend_strategy import TrendStrategy
    factory.register(TrendStrategy, {
        "description": "趋势跟踪策略（均线突破+ATR止损）",
        "market": ["US", "HK"],
        "type": "equity",
    })
except ImportError:
    pass

try:
    from strategies.equity.vwap_strategy import VWAPStrategy
    factory.register(VWAPStrategy, {
        "description": "VWAP均值回归策略",
        "market": ["US"],
        "type": "equity",
    })
except ImportError:
    pass

try:
    from strategies.equity.dual_thrust_strategy import DualThrustStrategy
    factory.register(DualThrustStrategy, {
        "description": "Dual Thrust 开盘区间突破策略",
        "market": ["US", "HK"],
        "type": "equity",
    })
except ImportError:
    pass

try:
    from strategies.futures.momentum_strategy import MomentumStrategy
    factory.register(MomentumStrategy, {
        "description": "期货动量突破策略",
        "market": ["US"],
        "type": "futures",
    })
except ImportError:
    pass
