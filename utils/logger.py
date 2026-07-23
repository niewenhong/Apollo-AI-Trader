# -*- coding: utf-8 -*-
"""统一日志封装：分级、远程上报、轮转"""
import logging
import json
import os
import sys
from datetime import datetime

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

def setup_logging(config_path: str = "config/logging_config.json"):
    """根据 JSON 配置初始化日志系统"""
    if os.path.exists(config_path):
        import json as _json
        with open(config_path, "r") as f:
            config = _json.load(f)
        # 简化版：直接用 dictConfig
        from logging.config import dictConfig
        dictConfig(config)
    else:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

def get_logger(name: str) -> logging.Logger:
    """获取 logger 实例"""
    return logging.getLogger(name)

class RemoteLogHandler(logging.Handler):
    """远程日志处理器（通过 HTTP 上报到 OpenClaw / Telegram）"""
    def __init__(self, endpoint: str = "", level=logging.WARNING):
        super().__init__(level=level)
        self.endpoint = endpoint

    def emit(self, record):
        try:
            msg = self.format(record)
            payload = {
                "time": datetime.now().isoformat(),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage()
            }
            # 实际发送逻辑由 monitoring/openclaw_client.py 接管
            # 这里仅做格式化
            if self.endpoint:
                import requests
                requests.post(self.endpoint, json=payload, timeout=3)
        except Exception:
            pass  # 日志失败不能影响主流程

# 默认初始化
setup_logging()
