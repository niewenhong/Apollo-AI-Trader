from vnpy.trader.setting import SETTINGS
from vnpy.trader.datafeed import BaseDatafeed

class FutuDatafeed(BaseDatafeed):
    """富途数据服务（占位）"""
    def query_bar_history(self, req):
        return None