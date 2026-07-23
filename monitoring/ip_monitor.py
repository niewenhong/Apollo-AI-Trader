# -*- coding: utf-8 -*-
"""
IP 监控器
- 绑定允许访问的 IP 列表
- 检测异常 IP 访问
- 记录访问日志
"""
import socket
import logging
from typing import set, Optional, List
from datetime import datetime

logger = logging.getLogger("monitoring.ip_monitor")


class IPMonitor:
    """IP 监控与绑定"""

    def __init__(self, allowed_ips: List[str] = None):
        self.allowed_ips: set = set(allowed_ips or [])
        self._access_log: List[dict] = []
        self._max_log = 1000
        self.local_ip = self._get_local_ip()
        self.hostname = socket.gethostname()

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def add_allowed_ip(self, ip: str):
        self.allowed_ips.add(ip)
        logger.info(f"[IP] 添加允许: {ip}")

    def set_allowed_ips(self, ips: List[str]):
        self.allowed_ips = set(ips)
        logger.info(f"[IP] 允许列表已更新: {ips}")

    def is_allowed(self, ip: str) -> bool:
        """检查 IP 是否允许"""
        if not self.allowed_ips:
            return True  # 空列表 = 放行所有
        return ip in self.allowed_ips

    def log_access(self, ip: str, command: str, allowed: bool):
        """记录访问"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "ip": ip,
            "command": command,
            "allowed": allowed
        }
        self._access_log.append(entry)
        if len(self._access_log) > self._max_log:
            self._access_log = self._access_log[-self._max_log:]

    def check_anomaly(self, ip: str) -> Optional[str]:
        """
        检测异常行为
        :return: 异常描述，None 表示正常
        """
        # 1. 未授权 IP
        if not self.is_allowed(ip):
            return f"未授权 IP: {ip}"

        # 2. 短时间内大量请求
        recent = [e for e in self._access_log
                   if e["ip"] == ip and
                   (datetime.now() - datetime.fromisoformat(e["timestamp"])).total_seconds() < 60]
        if len(recent) > 30:
            return f"IP {ip} 请求过于频繁: {len(recent)}/min"

        return None

    def get_access_log(self, limit: int = 50) -> List[dict]:
        return self._access_log[-limit:]

    def get_local_address(self) -> str:
        return f"{self.hostname}@{self.local_ip}"
