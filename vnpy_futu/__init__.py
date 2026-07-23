from .futu_gateway import FutuGateway

class Datafeed:
    """占位 Datafeed，避免 CtaStrategyApp 初始化时报错"""
    def query_bar_history(self, req):
        return None