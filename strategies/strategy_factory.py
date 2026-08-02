"""
strategies/strategy_factory.py - v2.9.4
策略工厂：统一注册、创建、管理所有策略类

v2.9.4 更新：
- 注册全部 17 个策略（equity 6 + futures 1 + options 8 + structured 2 + ipo 1）
- 每个策略标注 metadata（市场、类型、依赖数据）
- needs_tick / needs_kline 用于按需订阅
- v2.9.4：所有策略统一为只订阅 tick + 1M bar
"""
import importlib
import logging
from typing import Dict, Type, Any, Optional, List

from vnpy_ctastrategy import CtaTemplate

logger = logging.getLogger("StrategyFactory")


class StrategyFactory:
    """
    策略工厂：负责策略类的注册、发现和实例化。
    """
    def __init__(self):
        self._registry: Dict[str, Type[CtaTemplate]] = {}
        self._metadata: Dict[str, dict] = {}

    def register(self, cls: Type[CtaTemplate], metadata: Optional[dict] = None):
        name = cls.__name__
        if name in self._registry:
            logger.warning(f"[Factory] {name} 已注册，将被覆盖")
        self._registry[name] = cls
        self._metadata[name] = metadata or {}
        logger.info(f"[Factory] ✅ 注册策略: {name}")

    def get_class(self, class_name: str) -> Optional[Type[CtaTemplate]]:
        return self._registry.get(class_name)

    def create(self, cta_engine, strategy_name: str, vt_symbol: str,
               setting: dict) -> Optional[CtaTemplate]:
        class_name = setting.get("class_name", strategy_name)
        cls = self.get_class(class_name)
        if cls is None:
            logger.error(f"[Factory] 未找到策略类: {class_name}")
            return None
        try:
            return cls(cta_engine, strategy_name, vt_symbol, setting)
        except Exception as e:
            logger.error(f"[Factory] 创建 {strategy_name} 失败: {e}")
            return None

    def auto_discover(self, strategies_dir: str = "strategies"):
        import os, glob
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = os.path.join(base, strategies_dir, "**", "*.py")
        for f in glob.glob(pattern, recursive=True):
            if f.endswith("__init__.py") or f.endswith("strategy_factory.py"):
                continue
            self._try_import_file(f, base)

    def _try_import_file(self, filepath: str, base: str):
        import os
        rel = os.path.relpath(filepath, base)
        module_name = rel.replace(os.sep, ".").replace(".py", "")
        try:
            mod = importlib.import_module(module_name)
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(obj, CtaTemplate) and obj is not CtaTemplate:
                    self.register(obj)
        except Exception as e:
            logger.warning(f"[Factory] 导入 {module_name} 失败: {e}")

    def list_strategies(self, market: Optional[str] = None,
                        strategy_type: Optional[str] = None) -> List[dict]:
        result = []
        for name, cls in self._registry.items():
            meta = self._metadata.get(name, {})
            if market and market not in meta.get("market", []):
                continue
            if strategy_type and strategy_type != meta.get("type"):
                continue
            result.append({
                "name": name,
                "params": list(getattr(cls, 'parameters', [])),
                "description": meta.get("description", ""),
                "author": getattr(cls, 'author', 'Unknown'),
                "market": meta.get("market", []),
                "type": meta.get("type", ""),
                "needs_tick": meta.get("needs_tick", False),
                "needs_kline": meta.get("needs_kline", []),
            })
        return result

    def get_default_params(self, class_name: str) -> dict:
        cls = self.get_class(class_name)
        if cls is None:
            return {}
        params = getattr(cls, 'parameters', [])
        defaults = {}
        for p in params:
            defaults[p] = getattr(cls, p, None)
        return defaults

    def get_all_metadata(self) -> Dict[str, dict]:
        return dict(self._metadata)


# ========== 全局工厂实例 ==========
factory = StrategyFactory()

# ========== Equity 策略注册 ==========
try:
    from strategies.equity.multi_indicator_strategy import MultiIndicatorStrategy
    factory.register(MultiIndicatorStrategy, {
        "description": "10+维共振多指标策略（均线+RSI+MACD+布林+ATR+量能+ADX）",
        "market": ["US", "HK"],
        "type": "equity",
        "needs_tick": False,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.equity.order_flow_strategy import TickOrderFlowStrategy
    factory.register(TickOrderFlowStrategy, {
        "description": "Tick级订单流策略（盘口imbalance+Kelly+移动止盈）",
        "market": ["US", "HK"],
        "type": "equity",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.equity.grid_strategy import GridStrategy
    factory.register(GridStrategy, {
        "description": "网格交易策略（ATR动态间距+趋势过滤）",
        "market": ["US", "HK"],
        "type": "equity",
        "needs_tick": False,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.equity.trend_strategy import TrendStrategy
    factory.register(TrendStrategy, {
        "description": "趋势跟踪（多周期均线+ADX+ATR止损+Regime感知）",
        "market": ["US", "HK"],
        "type": "equity",
        "needs_tick": False,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.equity.vwap_strategy import VWAPStrategy
    factory.register(VWAPStrategy, {
        "description": "VWAP均值回归策略（Keltner通道+5M过滤）",
        "market": ["US", "HK"],
        "type": "equity",
        "needs_tick": False,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.equity.dual_thrust_strategy import DualThrustStrategy
    factory.register(DualThrustStrategy, {
        "description": "Dual Thrust 开盘区间突破（ATR区间+5M确认）",
        "market": ["US", "HK"],
        "type": "equity",
        "needs_tick": False,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

# ========== Futures 策略注册 ==========
try:
    from strategies.futures.momentum_strategy import MomentumStrategy
    factory.register(MomentumStrategy, {
        "description": "期货动量突破策略",
        "market": ["US"],
        "type": "futures",
        "needs_tick": False,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

# ========== Options 策略注册 ==========
try:
    from strategies.options.sell_call_strategy import SellCallStrategy
    factory.register(SellCallStrategy, {
        "description": "卖出看涨期权（Delta区间筛选+ADX趋势过滤+Tick快速展期）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.sell_put_strategy import SellPutStrategy
    factory.register(SellPutStrategy, {
        "description": "卖出看跌期权（Delta区间+真实现金检查+Tick漂移展期）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.cash_secured_put_strategy import CashSecuredPutStrategy
    factory.register(CashSecuredPutStrategy, {
        "description": "现金担保看跌期权（保守Delta+足额现金+Tick展期）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.covered_call_strategy import CoveredCallStrategy
    factory.register(CoveredCallStrategy, {
        "description": "备兑看涨期权（真实同步正股持仓+5M多头确认+Tick展期）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.bull_call_spread_strategy import BullCallSpreadStrategy
    factory.register(BullCallSpreadStrategy, {
        "description": "牛市看涨价差（Delta容差+5M EMA确认+Tick回撤平仓）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.bear_put_spread_strategy import BearPutSpreadStrategy
    factory.register(BearPutSpreadStrategy, {
        "description": "熊市看跌价差（对称修复+5M EMA空头确认）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.iron_condor_strategy import IronCondorStrategy
    factory.register(IronCondorStrategy, {
        "description": "铁鹰策略（真实行权价差+4腿回滚+Tick突破平仓+ADX震荡过滤）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.options.straddle_strategy import StraddleStrategy
    factory.register(StraddleStrategy, {
        "description": "跨式策略（事件检测+IV百分位+Tick快速止损+5M止盈止损）",
        "market": ["US"],
        "type": "options",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

# ========== Structured Products 策略注册 ==========
try:
    from strategies.structured_products.warrant_strategy import WarrantStrategy
    factory.register(WarrantStrategy, {
        "description": "窝轮/牛熊证统一策略（Delta筛选+5M EMA+ADX+盘口+距收回价防御）",
        "market": ["HK"],
        "type": "structured",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

try:
    from strategies.structured_products.cbbc_strategy import CBBCStrategy
    factory.register(CBBCStrategy, {
        "description": "牛熊证专用策略（距收回价防御+杠杆估值+Regime缩放+Tick实时检查）",
        "market": ["HK"],
        "type": "structured",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass

# ========== IPO 策略注册 ==========
try:
    from strategies.ipo.ipo_strategy import IPOStrategy
    factory.register(IPOStrategy, {
        "description": "新股申购+首日交易策略",
        "market": ["US", "HK"],
        "type": "ipo",
        "needs_tick": True,
        "needs_kline": ["1M"],
    })
except ImportError:
    pass
