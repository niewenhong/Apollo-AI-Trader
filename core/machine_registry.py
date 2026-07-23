# core/machine_registry.py
import socket
import uuid


class MachineRegistry:
    """机器标识注册器"""

    def __init__(self):
        self.hostname = socket.gethostname()
        try:
            self.ip = socket.gethostbyname(self.hostname)
        except socket.gaierror:
            self.ip = "127.0.0.1"
        self.machine_id = uuid.uuid4().hex[:8]

    def tag(self) -> str:
        return f"{self.machine_id}@{self.ip}"
