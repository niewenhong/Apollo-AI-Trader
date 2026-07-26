"""
core/machine_registry.py — 机器注册与集群心跳 v1.0.0

功能：
- 自动生成/复用唯一机器ID (MCH-XXXXXXXX)
- 支持集群标识、实例类型
- 心跳上报（本地文件 / Redis / HTTP API 三种模式）
- 多机发现（/cluster 命令查询同集群所有在线机器）
"""
import json
import os
import socket
import time
import uuid
import logging
from typing import Optional, Dict, List

logger = logging.getLogger("MachineRegistry")


class MachineRegistry:
    """
    机器注册表

    单实例对应一台机器上的一个运行实例。
    心跳数据写入本地文件（默认 data/machine_registry.json），
    也可通过 Redis 或 HTTP API 上报（配置驱动）。
    """

    def __init__(self, config: Optional[dict] = None):
        """
        :param config: cluster 配置段
            {
                "cluster_id": "prod-us-east",
                "instance_type": "CTA_US",
                "instance_name": "",          # 可选，自定义实例名
                "registry_file": "data/machine_registry.json",
                "heartbeat_interval": 30,
                "redis_url": "",
                "heartbeat_api": ""
            }
        """
        config = config or {}
        self.cluster_id = config.get("cluster_id", "default")
        self.instance_type = config.get("instance_type", "STANDALONE")
        self.instance_name = config.get("instance_name", "")
        self.registry_file = config.get("registry_file", "data/machine_registry.json")
        self.heartbeat_interval = config.get("heartbeat_interval", 30)
        self.redis_url = config.get("redis_url", "")
        self.heartbeat_api = config.get("heartbeat_api", "")

        # 机器ID（持久化到 registry_file）
        self.machine_id = self._load_or_create_machine_id()

        # 实例名（默认用 machine_id 后4位）
        if not self.instance_name:
            self.instance_name = f"{self.instance_type.lower()}-{self.machine_id[-4:]}"

        # 启动时间
        self.boot_time = time.time()

        # 心跳状态
        self._last_heartbeat = 0
        self._redis_client = None

        # 立即写入首次心跳
        self.heartbeat()

        logger.info(f"🏷️ 机器注册完成: [{self.cluster_id}][{self.machine_id}][{self.instance_type}]")
        mode = "redis" if self.redis_url else ("http" if self.heartbeat_api else "local")
        logger.info(f"   模式: {mode} | 实例: {self.instance_name}")

    # ──────────────────────────────
    #  机器ID 管理
    # ──────────────────────────────
    def _load_or_create_machine_id(self) -> str:
        """从 registry_file 加载已有ID，或生成新ID"""
        os.makedirs(os.path.dirname(self.registry_file) or ".", exist_ok=True)

        if os.path.exists(self.registry_file):
            try:
                with open(self.registry_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                mid = data.get("machine_id")
                if mid:
                    logger.info(f"   复用已有机器ID: {mid}")
                    return mid
            except (json.JSONDecodeError, OSError):
                pass

        # 生成新ID
        random_hex = uuid.uuid4().hex[:8].upper()
        mid = f"MCH-{random_hex}"
        logger.info(f"   生成新机器ID: {mid}")

        self._save_registry({"machine_id": mid})
        return mid

    def _save_registry(self, data: dict):
        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing = {}

        existing.update(data)
        with open(self.registry_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

    # ──────────────────────────────
    #  心跳
    # ──────────────────────────────
    def heartbeat(self) -> dict:
        """发送心跳，返回心跳数据"""
        now = time.time()
        uptime = now - self.boot_time
        h, m = divmod(int(uptime) // 60, 60)

        data = {
            "machine_id": self.machine_id,
            "cluster_id": self.cluster_id,
            "instance_type": self.instance_type,
            "instance_name": self.instance_name,
            "hostname": socket.gethostname(),
            "status": "online",
            "last_seen": now,
            "uptime_seconds": uptime,
            "uptime_human": f"{h}h{m}m",
            "boot_time": self.boot_time,
        }

        # 模式1：本地文件
        if not self.redis_url and not self.heartbeat_api:
            self._save_registry({"heartbeat": data})

        # 模式2：Redis
        elif self.redis_url:
            self._heartbeat_redis(data)

        # 模式3：HTTP API
        elif self.heartbeat_api:
            self._heartbeat_http(data)

        self._last_heartbeat = now
        return data

    def _heartbeat_redis(self, data: dict):
        try:
            if self._redis_client is None:
                import redis
                self._redis_client = redis.from_url(self.redis_url)
            key = f"apollo:cluster:{self.cluster_id}:{self.machine_id}"
            self._redis_client.hset(key, mapping={k: str(v) for k, v in data.items()})
            self._redis_client.expire(key, self.heartbeat_interval * 3)
        except Exception as e:
            logger.warning(f"Redis 心跳失败: {e}")

    def _heartbeat_http(self, data: dict):
        try:
            import requests
            requests.post(self.heartbeat_api, json=data, timeout=5)
        except Exception as e:
            logger.warning(f"HTTP 心跳失败: {e}")

    # ──────────────────────────────
    #  集群发现
    # ──────────────────────────────
    def discover_cluster(self) -> List[dict]:
        """发现同集群所有在线机器"""
        if self.redis_url:
            return self._discover_redis()
        else:
            return self._discover_local()

    def _discover_redis(self) -> List[dict]:
        try:
            if self._redis_client is None:
                import redis
                self._redis_client = redis.from_url(self.redis_url)
            pattern = f"apollo:cluster:{self.cluster_id}:*"
            results = []
            for key in self._redis_client.scan_iter(match=pattern):
                data = self._redis_client.hgetall(key)
                if data:
                    results.append({k.decode(): v.decode() for k, v in data.items()})
            return results
        except Exception as e:
            logger.warning(f"Redis 集群发现失败: {e}")
            return []

    def _discover_local(self) -> List[dict]:
        """本地模式：只返回自己"""
        return [self.heartbeat()]

    # ──────────────────────────────
    #  标识
    # ──────────────────────────────
    def tag(self) -> str:
        """返回机器标识字符串（供日志/通知使用）"""
        return f"[{self.cluster_id}][{self.machine_id}][{self.instance_type}]"

    def summary(self) -> str:
        """返回完整摘要"""
        uptime = time.time() - self.boot_time
        h, m = divmod(int(uptime) // 60, 60)
        return (f"[{self.cluster_id}][{self.machine_id}][{self.instance_type}] "
                f"name={self.instance_name} uptime={h}h{m}m")

    def should_heartbeat(self) -> bool:
        """是否应该发送心跳（基于间隔）"""
        return (time.time() - self._last_heartbeat) >= self.heartbeat_interval

    def mark_offline(self):
        """标记本机离线"""
        data = self.heartbeat()
        data["status"] = "offline"
        if self.redis_url:
            self._heartbeat_redis(data)
        else:
            self._save_registry({"heartbeat": data})
        logger.info(f"💤 机器标记为离线: {self.machine_id}")
