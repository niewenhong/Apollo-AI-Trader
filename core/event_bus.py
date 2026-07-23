# -*- coding: utf-8 -*-
"""事件总线（发布-订阅模式，解耦模块间通信）"""
import threading
from typing import Callable, Dict, List, Any
import logging

logger = logging.getLogger("core.event_bus")

class EventBus:
    """轻量级事件总线"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
        return cls._instance

    def _init(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable[[Any], None]):
        """订阅事件"""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
            logger.debug(f"订阅事件: {event_type}")

    def unsubscribe(self, event_type: str, callback: Callable):
        """取消订阅"""
        with self._lock:
            if event_type in self._subscribers:
                self._subscribers[event_type].remove(callback)

    def publish(self, event_type: str, data: Any = None):
        """发布事件（同步调用）"""
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            try:
                cb(data)
            except Exception as e:
                logger.error(f"事件回调错误 [{event_type}]: {e}")

    def publish_async(self, event_type: str, data: Any = None):
        """发布事件（异步线程）"""
        import threading
        t = threading.Thread(target=self.publish, args=(event_type, data), daemon=True)
        t.start()

# 预定义事件类型
class Events:
    """事件类型常量"""
    TICK = "tick"
    BAR = "bar"
    ORDER = "order"
    TRADE = "trade"
    SIGNAL = "signal"
    RISK_BREACH = "risk_breach"
    HEARTBEAT = "heartbeat"
    SYSTEM_ERROR = "system_error"
    CONFIG_CHANGED = "config_changed"
    ALERT = "alert"
