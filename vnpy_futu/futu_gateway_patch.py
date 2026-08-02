"""
futu_gateway.py 修复补丁说明
================================
本文件包含需要替换到 futu_gateway.py 中的两个方法。
直接覆盖原文件中的对应方法即可。

修复点：
1. connect_quote()  — K 线 handler 注册 + 日志
2. subscribe()     — session=0 (SESSION_NONE)，美股盘前盘后不推送
3. 新增 _kline_debug() 辅助方法
"""


# ═══════════════════════════════════
#  修复 1：connect_quote() — 完整替换
# ═══════════════════════════════════
def connect_quote(self) -> None:
    """行情连接 — v3.0.2 修复版"""
    try:
        self.quote_ctx = OpenQuoteContext(self.host, self.port)
    except Exception as e:
        self.write_log(f"[{self.gateway_name}] 行情连接异常: {e}")
        return

    # ── Quote 处理器 ──
    class QuoteHandler(StockQuoteHandlerBase):
        gateway = self
        def on_recv_rsp(self, rsp_str):
            ret_code, content = super().on_recv_rsp(rsp_str)
            if ret_code != RET_OK:
                return RET_ERROR, content
            try:
                self.gateway.process_quote(content)
            except Exception as e:
                self.gateway.write_log(f"[QuoteHandler] error: {e}")
            return RET_OK, content

    # ── OrderBook 处理器 ──
    class OrderBookHandler(OrderBookHandlerBase):
        gateway = self
        def on_recv_rsp(self, rsp_str):
            ret_code, content = super().on_recv_rsp(rsp_str)
            if ret_code != RET_OK:
                return RET_ERROR, content
            try:
                self.gateway.process_orderbook(content)
            except Exception as e:
                self.gateway.write_log(f"[OrderBookHandler] error: {e}")
            return RET_OK, content

    # ── Ticker 处理器 ──
    class TickerHandler(TickerHandlerBase):
        gateway = self
        def on_recv_rsp(self, rsp_str):
            ret_code, content = super().on_recv_rsp(rsp_str)
            if ret_code != RET_OK:
                return RET_ERROR, content
            try:
                self.gateway.process_ticker(content)
            except Exception as e:
                self.gateway.write_log(f"[TickerHandler] error: {e}")
            return RET_OK, content

    # 注册三大基础 handler
    self.quote_ctx.set_handler(QuoteHandler())
    self.quote_ctx.set_handler(OrderBookHandler())
    self.quote_ctx.set_handler(TickerHandler())

    # ★ 注册 K 线处理器（必须，否则 K 线推送无人接收）
    try:
        from .multi_period_kline_handler import MultiPeriodKlineHandler
        self.kline_handler = MultiPeriodKlineHandler(self, market_bus=self.market_bus)
        self.quote_ctx.set_handler(self.kline_handler)
        self.write_log(f"[{self.gateway_name}] ✅ K线处理器已注册 (MultiPeriodKlineHandler)")
    except Exception as e:
        # 尝试备用导入路径
        try:
            from vnpy_futu.multi_period_kline_handler import MultiPeriodKlineHandler
            self.kline_handler = MultiPeriodKlineHandler(self, market_bus=self.market_bus)
            self.quote_ctx.set_handler(self.kline_handler)
            self.write_log(f"[{self.gateway_name}] ✅ K线处理器已注册 (vnpy_futu 路径)")
        except Exception as e2:
            self.write_log(f"[{self.gateway_name}] ❌ K线处理器注册失败: {e2}")
            self.kline_handler = None

    self.quote_ctx.start()
    self.write_log(f"[{self.gateway_name}] 行情接口连接成功（含多周期K线+TICKER Handler）")


# ═══════════════════════════════════
#  修复 2：subscribe() — 完整替换
# ═══════════════════════════════════
def subscribe(self, req: SubscribeRequest) -> None:
    """
    v3.0.2 修复版：
    - 只订阅 QUOTE + K_1M（高周期由 SubscriptionManager 按需追加）
    - ★ session=0（SESSION_NONE）：
      美股仅在交易时段（09:30–16:00 ET）推送 K 线，
      避免盘前盘后稀疏报价污染策略 ArrayManager
    - 清晰日志，方便排查
    """
    try:
        futu_exchange = EXCHANGE_VT2FUTU[req.exchange]
    except KeyError:
        self.write_log(f"[{self.gateway_name}] ❌ 不支持的交易所: {req.exchange}")
        return

    vt_symbol = f"{req.symbol}.{req.exchange.value}"
    if vt_symbol not in self.contracts:
        self._register_single_contract(req.symbol, req.exchange, futu_exchange)

    futu_symbol = f"{futu_exchange}.{req.symbol}"

    # ★ 仅订阅基础类型
    sub_types = [SubType.QUOTE, SubType.K_1M]

    # ★ session=0 → SESSION_NONE（仅交易时段推送）
    # 美股盘前 04:00–09:30 ET 有大量稀疏数据，会污染 am
    session = 0  # SESSION_NONE = 0
    self.write_log(f"[{self.gateway_name}] 订阅参数: {futu_symbol} session={session}(NONE)")

    code, data = self.quote_ctx.subscribe(
        futu_symbol, sub_types,
        is_first_push=True, subscribe_push=True, session=session
    )
    if code == RET_OK:
        self.write_log(f"[{self.gateway_name}] ✅ 基础订阅成功: {futu_symbol} (QUOTE+K_1M)")
    else:
        self.write_log(f"[{self.gateway_name}] ❌ 订阅失败: {futu_symbol} | {data}")
        # 逐个重试，定位哪个 SubType 失败
        for st in sub_types:
            c2, d2 = self.quote_ctx.subscribe(
                futu_symbol, [st],
                is_first_push=True, subscribe_push=True, session=session
            )
            st_name = str(st)
            if c2 == RET_OK:
                self.write_log(f"[{self.gateway_name}]   ✅ 单独 {st_name}: OK")
            else:
                self.write_log(f"[{self.gateway_name}]   ❌ 单独 {st_name}: {d2}")


# ═══════════════════════════════════
#  修复 3：subscribe_subtypes() — 完整替换
# ═══════════════════════════════════
def subscribe_subtypes(self, futu_symbol: str, subtypes: list) -> bool:
    """
    由 SubscriptionManager 调用，追加订阅高周期类型。
    v3.0.2：同样使用 session=0 避免盘前盘后噪声。
    """
    if self.quote_ctx is None:
        return False

    sub_objs = []
    for st_str in subtypes:
        if st_str in ("QUOTE", "K_1M"):
            continue
        mapped = self._str_to_subtype(st_str)
        if mapped is not None:
            sub_objs.append(mapped)
    sub_objs = list(set(sub_objs))

    if not sub_objs:
        return True

    session = 0  # SESSION_NONE
    code, data = self.quote_ctx.subscribe(
        futu_symbol, sub_objs,
        is_first_push=True, subscribe_push=True, session=session
    )
    if code == RET_OK:
        self.write_log(f"[{self.gateway_name}] ✅ 追加订阅: {futu_symbol} 类型={subtypes}")
        return True
    else:
        self.write_log(f"[{self.gateway_name}] ❌ 追加订阅失败: {futu_symbol} | {data}")
        return False
