"""
strategies/__init__.py - v3.8.0
策略包初始化
"""
from strategies.base_strategy import BaseStrategy
from strategies.strategy_factory import StrategyFactory

__version__ = "3.8.0"
__all__ = ['BaseStrategy', 'StrategyFactory']
