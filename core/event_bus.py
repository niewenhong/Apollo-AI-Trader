# -*- coding: utf-8 -*-
"""事件总线（发布-订阅模式，解耦模块间通信）

改造说明：支持按 engine_id 获取独立 EventBus 实例（EventBus(engine_id) 或 EventBus.get_bus(engine_id)），
保持向后兼容：EventBus() 或 EventBus.get_bus() 返回默认 'default' 实例。
"""
import threading
from typing import Callable, Dict, List, Any
import logging

logger = logging.getLogger("core.event_bus")


class EventBus:
    """轻量级事件总线，支持按 engine_id 隔离实例"""
    _instances: Dict[str, "EventBus"] = {}
    _global_lock = threading.Lock()

    def __new__(cls, engine_id: str = "default"):
        # return or create an instance per engine_id
        with cls._global_lock:
            if engine_id not in cls._instances:
                inst = super().__new__(cls)
                inst._engine_id = engine_id
                inst._init()
                cls._instances[engine_id] = inst
        return cls._instances[engine_id]

    @classmethod
    def get_bus(cls, engine_id: str = "default") -> "EventBus":
        """兼容方法：显式按 engine_id 获取 EventBus 实例"""
        return cls(engine_id)

    def _init(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable[[Any], None]):
        """订阅事件"""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
            logger.debug(f"[{getattr(self,'_engine_id', 'default')}] 订阅事件: {event_type}")

    def unsubscribe(self, event_type: str, callback: Callable):
        """取消订阅"""
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                except ValueError:
                    # 回调不在列表中，忽略
                    pass

    def publish(self, event_type: str, data: Any = None):
        """发布事件（同步调用）"""
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            try:
                cb(data)
            except Exception as e:
                logger.error(f"[{getattr(self,'_engine_id', 'default')}] 事件回调错误 [{event_type}]: {e}")

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
