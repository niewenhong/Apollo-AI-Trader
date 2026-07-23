"""
machine_registry.py — Apollo-AI-Tra-der v2.4.0
机器唯一标识生成器（短哈希 + 局域网 IP）
"""
import hashlib
import socket
import uuid

def get_local_ip() -> str:
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

class MachineRegistry:
    """为每台机器生成稳定、短小、唯一的标识"""

    def __init__(self):
        self.mac = self._get_mac()
        self.ip = get_local_ip()
        self.hostname = socket.gethostname()
        self.machine_id = self._gen_id()

    def _get_mac(self) -> str:
        try:
            mac = uuid.getnode()
            return ':'.join(f'{(mac >> (8*i)) & 0xff:02x}' for i in range(5, -1, -1))
        except Exception:
            return "00:00:00:00:00:00"

    def _gen_id(self) -> str:
        raw = f"{self.mac}-{self.hostname}-{self.ip}"
        h = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"{h}@{self.ip}"

    def tag(self) -> str:
        """返回人类可读的标识"""
        return self.machine_id

    def short_tag(self) -> str:
        """返回更短的标识（用于日志前缀）"""
        return self.machine_id.split('@')[0]
