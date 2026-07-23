"""
vnpy_futu — Apollo-AI-Tra-der v2.5.0-DEBUG
"""
from vnpy_futu.futu_gateway import FutuGateway

# FutuDatafeed 占位（兼容旧导入）
class FutuDatafeed:
    """富途数据导入（占位类）"""
    def __init__(self):
        self.name = "FutuDatafeed"

__all__ = ["FutuGateway", "FutuDatafeed"]
