"""
vnpy_localdata - Apollo 本地数据服务 v1.0.0
将本地数据库中存储的富途K线数据接入 vnpy datafeed 体系
"""
from .datafeed import LocalDatafeed

# vnpy datafeed 工厂约定：模块必须暴露 Datafeed 类
Datafeed = LocalDatafeed
__all__ = ["LocalDatafeed", "Datafeed"]
