"""
strategies/structured_products/__init__.py - v2.9.7
注册结构化产品策略
"""
from .warrant_strategy import WarrantStrategy
from .cbbc_strategy import CBBCStrategy

__all__ = ["WarrantStrategy", "CBBCStrategy"]
