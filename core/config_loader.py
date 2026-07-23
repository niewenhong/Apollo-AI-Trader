# -*- coding: utf-8 -*-
"""热配置加载器（支持文件变化监听）"""
import json
import os
import threading
from typing import Callable, Dict, Any

class ConfigChangeHandler:
    """配置文件变更处理器"""
    def __init__(self, callback: Callable[[str], None]):
        self.callback = callback

    def on_modified(self, event):
        if event.src_path.endswith('.json'):
            try:
                self.callback(event.src_path)
            except Exception as e:
                print(f"Config callback error: {e}")

class ConfigLoader:
    """配置加载器（单例），支持热加载"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, config_dir: str = "config"):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init(config_dir)
        return cls._instance

    def _init(self, config_dir: str):
        self.config_dir = config_dir
        self._callbacks = []
        self._cache: Dict[str, dict] = {}
        self._start_watcher()

    def _start_watcher(self):
        """启动文件监听（watchdog）"""
        try:
            from watchdog.observers import Observer
            path = os.path.join(os.getcwd(), self.config_dir)
            if not os.path.exists(path):
                return
            event_handler = ConfigChangeHandler(self._on_config_changed)
            observer = Observer()
            observer.schedule(event_handler, path, recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
        except ImportError:
            # watchdog 未安装，降级为手动加载
            pass

    def _on_config_changed(self, filepath: str):
        """配置文件变更回调"""
        for cb in self._callbacks:
            try:
                cb(filepath)
            except Exception as e:
                print(f"Callback error: {e}")

    def register_callback(self, callback: Callable[[str], None]):
        """注册配置变更回调"""
        self._callbacks.append(callback)

    def load(self, name: str) -> dict:
        """
        加载配置（带缓存）
        :param name: 配置文件名（不含路径，如 'vwap_config'）
        """
        filepath = self._resolve_path(name)
        if not os.path.exists(filepath):
            return {}
        mtime = os.path.getmtime(filepath)
        cache_key = filepath
        cached = self._cache.get(cache_key)
        if cached and cached.get('_mtime') == mtime:
            return cached.get('data', {})
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self._cache[cache_key] = {'data': data, '_mtime': mtime}
        return data

    def save(self, name: str, data: dict):
        """保存配置"""
        filepath = self._resolve_path(name)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        # 更新缓存
        self._cache[filepath] = {'data': data, '_mtime': os.path.getmtime(filepath)}

    def _resolve_path(self, name: str) -> str:
        """解析配置文件路径"""
        if name.endswith('.json'):
            filename = name
        elif name.endswith('_config'):
            filename = f"{name}.json"
        else:
            filename = f"{name}_config.json"
        # 尝试 strategies 子目录
        sub = os.path.join(self.config_dir, "strategies", filename)
        if os.path.exists(sub):
            return sub
        return os.path.join(self.config_dir, filename)

    def reload_all(self):
        """强制重新加载所有配置"""
        self._cache.clear()
