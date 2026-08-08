"""
strategies/strategy_factory.py - v3.8.0
策略工厂：注册与创建策略实例

v3.8.0 变更：
- 支持用户级策略注册
- 系统级策略可共享
"""
import logging
import importlib
from typing import Dict, Type, Optional, Any

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("StrategyFactory")


class StrategyFactory:
    """
    策略工厂

    职责：
    - 注册策略类（系统级 + 用户级）
    - 按名称创建策略实例
    - 管理策略可见性（用户级 vs 系统级）
    """

    # 系统级策略注册表
    _system_registry: Dict[str, Type[BaseStrategy]] = {}

    # 用户级策略注册表: user_id → {name: class}
    _user_registry: Dict[str, Dict[str, Type[BaseStrategy]]] = {}

    @classmethod
    def register_system(cls, name: str, strategy_class: Type[BaseStrategy]):
        """注册系统级策略（所有用户可用）"""
        cls._system_registry[name] = strategy_class
        logger.info(f"[Factory] 🌐 注册系统策略: {name}")

    @classmethod
    def register_user(cls, user_id: str, name: str,
                      strategy_class: Type[BaseStrategy]):
        """注册用户级策略"""
        if user_id not in cls._user_registry:
            cls._user_registry[user_id] = {}
        cls._user_registry[user_id][name] = strategy_class
        logger.info(f"[Factory] 👤 注册用户策略: {user_id}/{name}")

    @classmethod
    def get_class(cls, name: str, user_id: str = "SYSTEM") -> Optional[Type[BaseStrategy]]:
        """获取策略类（优先用户级，后系统级）"""
        # 1. 用户级
        user_classes = cls._user_registry.get(user_id, {})
        if name in user_classes:
            return user_classes[name]

        # 2. 系统级
        if name in cls._system_registry:
            return cls._system_registry[name]

        # 3. 尝试动态导入
        return cls._dynamic_import(name)

    @classmethod
    def get_system_classes(cls) -> Dict[str, Type[BaseStrategy]]:
        """获取所有系统级策略类"""
        return dict(cls._system_registry)

    @classmethod
    def get_user_classes(cls, user_id: str) -> Dict[str, Type[BaseStrategy]]:
        """获取某用户的所有策略类"""
        return dict(cls._user_registry.get(user_id, {}))

    @classmethod
    def get_visible_classes(cls, user_id: str) -> Dict[str, Type[BaseStrategy]]:
        """获取用户可见的所有策略类（系统 + 自己的）"""
        visible = {}
        visible.update(cls._system_registry)  # 系统策略
        visible.update(cls._user_registry.get(user_id, {}))  # 用户自己的
        return visible

    @classmethod
    def create(cls, name: str, cta_engine, strategy_name: str,
                vt_symbol: str, setting: dict,
                user_id: str = "SYSTEM") -> Optional[BaseStrategy]:
        """创建策略实例"""
        strategy_class = cls.get_class(name, user_id)
        if not strategy_class:
            logger.error(f"[Factory] 策略类未找到: {name} (user={user_id})")
            return None

        try:
            instance = strategy_class(cta_engine, strategy_name, vt_symbol, setting)
            # 注入 user_id
            instance.user_id = user_id
            return instance
        except Exception as e:
            logger.error(f"[Factory] 创建 {name} 失败: {e}", exc_info=True)
            return None

    @classmethod
    def list_all(cls) -> Dict[str, list]:
        """列出所有已注册策略"""
        result = {
            'system': list(cls._system_registry.keys()),
            'users': {
                uid: list(classes.keys())
                for uid, classes in cls._user_registry.items()
            }
        }
        return result

    @classmethod
    def unregister_user_strategy(cls, user_id: str, name: str):
        """注销用户策略"""
        if user_id in cls._user_registry:
            cls._user_registry[user_id].pop(name, None)
            logger.info(f"[Factory] 🗑️ 注销: {user_id}/{name}")

    @classmethod
    def _dynamic_import(cls, name: str) -> Optional[Type[BaseStrategy]]:
        """动态导入策略类"""
        # 搜索路径
        search_paths = [
            f"strategies.equity.{name.lower()}",
            f"strategies.futures.{name.lower()}",
            f"strategies.options.{name.lower()}",
            f"strategies.ipo.{name.lower()}",
            f"strategies.structured_products.{name.lower()}",
        ]

        for module_path in search_paths:
            try:
                module = importlib.import_module(module_path)
                cls_obj = getattr(module, name, None)
                if cls_obj and issubclass(cls_obj, BaseStrategy):
                    # 自动注册为系统级
                    cls._system_registry[name] = cls_obj
                    logger.info(f"[Factory] 🔍 动态导入: {name}")
                    return cls_obj
            except (ImportError, AttributeError):
                continue

        return None


# ─── 自动注册系统级策略 ───
def auto_register_all():
    """启动时自动注册所有系统级策略"""
    import strategies.equity
    import strategies.futures
    import strategies.options
    import strategies.ipo
    import strategies.structured_products

    # 扫描各模块
    modules = [
        ('strategies.equity', [
            'TrendStrategy', 'MomentumStrategy', 'VWAPStrategy',
            'GridStrategy', 'ScalpingStrategy', 'OrderFlowStrategy',
            'MultiIndicatorStrategy', 'ManagedPositionStrategy',
        ]),
        ('strategies.futures', ['MomentumStrategy']),
        ('strategies.options', [
            'BaseOptionStrategy', 'SellCallStrategy', 'SellPutStrategy',
            'CoveredCallStrategy', 'CashSecuredPutStrategy',
            'BullCallSpreadStrategy', 'BearPutSpreadStrategy',
            'IronCondorStrategy', 'StraddleStrategy',
        ]),
        ('strategies.ipo', ['IPOStrategy']),
        ('strategies.structured_products', ['CBBCStrategy', 'WarrantStrategy']),
    ]

    for module_name, class_names in modules:
        try:
            module = importlib.import_module(module_name)
            for cls_name in class_names:
                cls_obj = getattr(module, cls_name, None)
                if cls_obj and issubclass(cls_obj, BaseStrategy):
                    StrategyFactory.register_system(cls_name, cls_obj)
        except ImportError:
            continue

    logger.info(f"[Factory] ✅ 自动注册完成: "
                f"{len(StrategyFactory.get_system_classes())} 个系统策略")
