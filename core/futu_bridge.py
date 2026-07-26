"""
core/futu_bridge.py - 富途上下文桥接
用于在 LocalDatafeed 中访问 quote_ctx（无需全局变量污染）
"""
from typing import Optional
from futu import OpenQuoteContext

_quote_ctx: Optional[OpenQuoteContext] = None

def set_quote_ctx(ctx: OpenQuoteContext):
    """在 main.py 中连接网关后调用，注入 quote_ctx"""
    global _quote_ctx
    _quote_ctx = ctx

def get_quote_ctx() -> Optional[OpenQuoteContext]:
    """LocalDatafeed 回源时获取 quote_ctx"""
    return _quote_ctx
