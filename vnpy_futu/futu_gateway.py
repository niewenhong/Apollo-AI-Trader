"""
futu_gateway.py — Apollo-AI-Trader v2.4.0
富途网关（双 Gateway 架构，每市场独立实例）
修正：
  1. send_order 直接把裸码/带前缀码传给 place_order
     （富途 place_order 两种都接受，关键是 gateway 内部 filter_trdmarket 已定市场）
  2. send_order 成功后，立即构造 OrderData 并写入 self.orders，
     保证撤单时本地缓存一定能查到
  3. on_order 回调也写 self.orders，保持同步
  4. account 用 cash 字段；frozen = total_assets - cash
"""
import pandas as pd
from copy import copy
from datetime import datetime
from threading import Thread
from time import sleep
from typing import Any, Dict, List, Set, Tuple, Union

from futu import (
    ModifyOrderOp, TrdSide, TrdEnv, TrdMarket, KLType,
    OpenQuoteContext, OrderBookHandlerBase, OrderStatus, OrderType as FutuOrderType,
    RET_ERROR, RET_OK, StockQuoteHandlerBase,
    TradeDealHandlerBase, TradeOrderHandlerBase,
    OpenSecTradeContext, OpenFutureTradeContext,
)

from vnpy.event import EventEngine
from vnpy.trader.constant import (
    Direction, Exchange, Offset, Product, Status, Interval,
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    TickData, OrderData, TradeData, BarData,
    AccountData, ContractData, PositionData,
    SubscribeRequest, OrderRequest, CancelRequest, HistoryRequest,
)
from vnpy.trader.event import EVENT_TIMER
from vnpy.trader.utility import ZoneInfo


# ========== 状态映射 ==========
STATUS_FUTU2VT: Dict = {
    OrderStatus.NONE: Status.SUBMITTING,
    OrderStatus.WAITING_SUBMIT: Status.SUBMITTING,
    OrderStatus.SUBMITTING: Status.SUBMITTING,
    OrderStatus.SUBMITTED: Status.NOTTRADED,
    OrderStatus.FILLED_PART: Status.PARTTRADED,
    OrderStatus.FILLED_ALL: Status.ALLTRADED,
    OrderStatus.CANCELLED_PART: Status.CANCELLED,
    OrderStatus.CANCELLED_ALL: Status.CANCELLED,
    OrderStatus.FAILED: Status.REJECTED,
    OrderStatus.DISABLED: Status.CANCELLED,
    OrderStatus.DELETED: Status.CANCELLED,
}

DIRECTION_VT2FUTU: Dict = {
    Direction.LONG: TrdSide.BUY,
    Direction.SHORT: TrdSide.SELL,
}
DIRECTION_FUTU2VT: Dict = {
    TrdSide.BUY: (Direction.LONG, Offset.OPEN),
    TrdSide.SELL: (Direction.SHORT, Offset.OPEN),
    TrdSide.BUY_BACK: (Direction.LONG, Offset.CLOSE),
    TrdSide.SELL_SHORT: (Direction.SHORT, Offset.CLOSE),
}

EXCHANGE_VT2FUTU: Dict = {
    Exchange.SMART: "US",
    Exchange.SEHK: "HK",
    Exchange.HKFE: "HK_FUTURE",
    Exchange.NASDAQ: "US",
    Exchange.NYSE: "US",
    Exchange.AMEX: "US",
    Exchange.NYMEX: "US",
    Exchange.COMEX: "US",
}
EXCHANGE_FUTU2VT: Dict = {v: k for k, v in EXCHANGE_VT2FUTU.items()}

PRODUCT_VT2FUTU: Dict = {
    Product.EQUITY: "STOCK",
    Product.INDEX: "IDX",
    Product.ETF: "ETF",
    Product.WARRANT: "WARRANT",
    Product.BOND: "BOND",
    Product.FUTURES: "FUTURE",
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")


class FutuGateway(BaseGateway):
    """富途网关（双 Gateway 架构，每市场独立实例）"""

    default_name: str = "FUTU"

    default_setting: Dict[str, Any] = {
        "密码": "",
        "地址": "127.0.0.1",
        "端口": 11111,
        "市场": "US",
        "环境": TrdEnv.SIMULATE,
    }

    exchanges: List[str] = list(EXCHANGE_FUTU2VT.values())

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        # 兼容 BaseGateway 是否有 put_event
        if not hasattr(self, 'put_event') or self.put_event is None:
            self.put_event = self.event_engine.put
        self.quote_ctx: OpenQuoteContext = None
        self.trade_ctx: Union[OpenSecTradeContext, OpenFutureTradeContext] = None
        self.host: str = ""
        self.port: int = 0
        self.market: str = "US"
        self.password: str = ""
        self.env: TrdEnv = TrdEnv.SIMULATE
        self.ticks: Dict[str, TickData] = {}
        # ★ 关键：本地订单缓存，key=vt_orderid，撤单靠它
        self.orders: Dict[str, OrderData] = {}
        self.trades: Set = set()
        self.contracts: Dict[str, ContractData] = {}
        self.thread: Thread = Thread(target=self.query_data, daemon=True)
        self.count: int = 0
        self.interval: int = 3
        self.query_funcs: list = [self.query_account, self.query_position]

    # ========== 连接 ==========
    def connect(self, setting: dict) -> None:
        self.host = setting.get("地址", "127.0.0.1")
        self.port = int(setting.get("端口", 11111))
        self.market = setting.get("市场", "US")
        self.password = setting.get("密码", "")
        self.env = setting.get("环境", TrdEnv.SIMULATE)
        self.connect_quote()
        self.connect_trade()
        self.thread.start()

    def query_data(self) -> None:
        sleep(2.0)
        self.query_contract()
        self.query_trade()
        self.query_order()
        self.query_position()
        self.query_account()
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_timer_event(self, event) -> None:
        self.count += 1
        if self.count < self.interval:
            return
        self.count = 0
        func = self.query_funcs.pop(0)
        func()
        self.query_funcs.append(func)

    # ========== 行情 ==========
    def connect_quote(self) -> None:
        self.quote_ctx = OpenQuoteContext(self.host, self.port)

        class QuoteHandler(StockQuoteHandlerBase):
            gateway = self
            def on_recv_rsp(self, rsp_str):
                ret_code, content = super().on_recv_rsp(rsp_str)
                if ret_code != RET_OK:
                    return RET_ERROR, content
                self.gateway.process_quote(content)
                return RET_OK, content

        class OrderBookHandler(OrderBookHandlerBase):
            gateway = self
            def on_recv_rsp(self, rsp_str):
                ret_code, content = super().on_recv_rsp(rsp_str)
                if ret_code != RET_OK:
                    return RET_ERROR, content
                self.gateway.process_orderbook(content)
                return RET_OK, content

        self.quote_ctx.set_handler(QuoteHandler())
        self.quote_ctx.set_handler(OrderBookHandler())
        self.quote_ctx.start()
        self.write_log(f"[{self.gateway_name}] 行情接口连接成功")

    # ========== 交易 ==========
    def connect_trade(self) -> None:
        if self.market == "HK":
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.HK, host=self.host, port=self.port)
        elif self.market == "US":
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US, host=self.host, port=self.port)
        elif self.market == "HK_FUTURE":
            self.trade_ctx = OpenFutureTradeContext(host=self.host, port=self.port)

        class OrderHandler(TradeOrderHandlerBase):
            gateway = self
            def on_recv_rsp(self, rsp_str):
                ret_code, content = super().on_recv_rsp(rsp_str)
                if ret_code != RET_OK:
                    return RET_ERROR, content
                self.gateway.process_order(content)
                return RET_OK, content

        class DealHandler(TradeDealHandlerBase):
            gateway = self
            def on_recv_rsp(self, rsp_str):
                ret_code, content = super().on_recv_rsp(rsp_str)
                if ret_code != RET_OK:
                    return RET_ERROR, content
                self.gateway.process_deal(content)
                return RET_OK, content

        # 解锁（GUI 版 OpenD 可能屏蔽，try 住）
        try:
            code, data = self.trade_ctx.unlock_trade(self.password)
            if code == RET_OK:
                self.write_log(f"[{self.gateway_name}] 交易接口解锁成功")
            else:
                self.write_log(f"[{self.gateway_name}] 交易接口解锁提示: {data}")
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 交易解锁接口不可用: {e}")

        self.trade_ctx.set_handler(OrderHandler())
        self.trade_ctx.set_handler(DealHandler())
        self.trade_ctx.start()
        self.write_log(f"[{self.gateway_name}] 交易接口连接成功")

    # ========== 订阅 ==========
    def subscribe(self, req: SubscribeRequest) -> None:
        try:
            futu_exchange = EXCHANGE_VT2FUTU[req.exchange]
        except KeyError:
            self.write_log(f"不支持的交易所: {req.exchange}")
            return
        futu_symbol = f"{futu_exchange}.{req.symbol}"
        for dtype in ["QUOTE", "ORDER_BOOK"]:
            code, data = self.quote_ctx.subscribe(futu_symbol, dtype, True)
            if code == RET_OK:
                self.write_log(f"[{self.gateway_name}] 订阅行情成功: {futu_symbol}")
            else:
                self.write_log(f"[{self.gateway_name}] 订阅行情失败: {futu_symbol} | {data}")

    # ========== ★ 下单（核心修正）==========
    def send_order(self, req: OrderRequest) -> str:
        side = DIRECTION_VT2FUTU[req.direction]
        adj = 0.05 if req.direction is Direction.LONG else -0.05

        # ★ 关键：直接用 req.symbol，不再拼接
        # 实测证明 "US.AAPL" 和 "AAPL" 富途都接受（市场由 filter_trdmarket 决定）
        futu_symbol = req.symbol

        code, data = self.trade_ctx.place_order(
            req.price, req.volume, futu_symbol, side,
            FutuOrderType.NORMAL, trd_env=self.env, adjust_limit=adj,
        )
        if code:
            self.write_log(f"[{self.gateway_name}] 委托失败: {data}")
            return ""

        # ★ 从富途返回里取 order_id
        orderid = ""
        for _, row in data.iterrows():
            orderid = str(row["order_id"])

        if not orderid:
            self.write_log(f"[{self.gateway_name}] 下单返回空 orderid")
            return ""

        # ★ 立即构造 OrderData 并写入 self.orders
        # 这样撤单时本地缓存一定能查到
        order = OrderData(
            symbol=req.symbol,           # 保持原样（"US.AAPL" 或 "AAPL"）
            exchange=req.exchange,
            orderid=orderid,             # 富途原始 order_id
            direction=req.direction,
            offset=req.offset,
            price=req.price,
            volume=req.volume,
            traded=0,
            status=Status.SUBMITTING,
            gateway_name=self.gateway_name,
            datetime=datetime.now(CHINA_TZ),
        )
        # vt_orderid = gateway_name + "." + orderid
        order.vt_orderid = f"{self.gateway_name}.{orderid}"
        order.reference = req.reference

        # ★ 写入本地缓存（双重 key，兼容不同查找方式）
        self.orders[order.vt_orderid] = order
        self.orders[orderid] = order  # 也用裸 orderid 存一份

        self.write_log(f"[{self.gateway_name}] 委托成功: {order.vt_orderid} {req.symbol} {req.volume}@{req.price}")

        # 触发事件
        self.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        code, data = self.trade_ctx.modify_order(
            ModifyOrderOp.CANCEL, req.orderid, 0, 0, trd_env=self.env)
        if code:
            self.write_log(f"[{self.gateway_name}] 撤单失败: {data}")
        else:
            self.write_log(f"[{self.gateway_name}] 撤单请求已发送: {req.orderid}")

    # ========== 账户查询 ==========
    def query_account(self) -> None:
        code, data = self.trade_ctx.accinfo_query(trd_env=self.env, acc_id=0)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询账户资金失败: {data}")
            return
        for _, row in data.iterrows():
            total_assets = float(row["total_assets"])
            cash = float(row.get("cash", total_assets))
            frozen = total_assets - cash
            self.on_account(AccountData(
                accountid=f"{self.gateway_name}",
                balance=total_assets,
                frozen=frozen,
                gateway_name=self.gateway_name,
            ))
            self.write_log(
                f"[{self.gateway_name}] 账户: 总资产=${total_assets:,.2f} "
                f"冻结=${frozen:,.2f} 可用=${cash:,.2f}")

    # ========== 持仓 ==========
    def query_position(self) -> None:
        code, data = self.trade_ctx.position_list_query(trd_env=self.env, acc_id=0)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询持仓失败: {data}")
            return
        for _, row in data.iterrows():
            symbol, exchange = convert_symbol_futu2vt(row["code"])
            self.on_position(PositionData(
                symbol=symbol, exchange=exchange, direction=Direction.NET,
                volume=int(row["qty"]),
                frozen=float(row["qty"]) - float(row.get("can_sell_qty", row["qty"])),
                price=float(row["cost_price"]),
                pnl=float(row["pl_val"]),
                gateway_name=self.gateway_name,
            ))

    # ========== 委托/成交 ==========
    def query_order(self) -> None:
        code, data = self.trade_ctx.order_list_query("", trd_env=self.env)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询委托失败: {data}")
            return
        self.process_order(data)

    def query_trade(self) -> None:
        code, data = self.trade_ctx.deal_list_query("", trd_env=self.env)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询成交失败: {data}")
            return
        self.process_deal(data)

    # ========== 合约 ==========
    def query_contract(self) -> None:
        market = "HK" if self.market in ["HK", "HK_FUTURE"] else self.market
        count = 0
        for product, futu_product in PRODUCT_VT2FUTU.items():
            code, data = self.quote_ctx.get_stock_basicinfo(market, futu_product)
            if code:
                continue
            for _, row in data.iterrows():
                symbol, exchange = convert_symbol_futu2vt(row["code"])
                contract = ContractData(
                    symbol=symbol, exchange=exchange, name=row["name"],
                    product=product, size=1, pricetick=0.001,
                    history_data=True, net_position=True,
                    gateway_name=self.gateway_name,
                )
                self.on_contract(contract)
                self.contracts[contract.vt_symbol] = contract
                count += 1
        self.write_log(f"[{self.gateway_name}] 合约查询完成: {count} 个")

    # ========== 关闭 ==========
    def close(self) -> None:
        if self.quote_ctx:
            self.quote_ctx.close()
        if self.trade_ctx:
            self.trade_ctx.close()

    # ========== Tick/历史 ==========
    def get_tick(self, code_str) -> TickData:
        tick = self.ticks.get(code_str)
        symbol, exchange = convert_symbol_futu2vt(code_str)
        if not tick:
            tick = TickData(
                symbol=symbol, exchange=exchange,
                datetime=datetime.now(CHINA_TZ),
                gateway_name=self.gateway_name,
            )
            self.ticks[code_str] = tick
        contract = self.contracts.get(tick.vt_symbol)
        if contract:
            tick.name = contract.name
        return tick

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        bars: List[BarData] = []
        if req.interval != Interval.MINUTE:
            self.write_log(f"[{self.gateway_name}] FUTU仅支持分钟线")
            return bars
        futu_symbol = f"{EXCHANGE_VT2FUTU.get(req.exchange, 'US')}.{req.symbol}"
        start = req.start.replace(tzinfo=None).strftime("%Y-%m-%d")
        end = req.end.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        ret, df, key = self.quote_ctx.request_history_kline(
            code=futu_symbol, start=start, end=end, ktype=KLType.K_1M)
        if ret != RET_OK:
            self.write_log(f"[{self.gateway_name}] 获取K线失败: {df}")
            return bars
        while key is not None:
            ret, more, key = self.quote_ctx.request_history_kline(
                code=futu_symbol, start=start, end=end,
                ktype=KLType.K_1M, page_req_key=key)
            if ret == RET_OK:
                df = pd.concat([df, more], ignore_index=True)
        df["time_key"] = pd.to_datetime(df["time_key"]) - pd.Timedelta(1, "m")
        for _, row in df.iterrows():
            bars.append(BarData(
                gateway_name=self.gateway_name,
                symbol=req.symbol, exchange=req.exchange,
                datetime=generate_datetime(row["time_key"].strftime("%Y-%m-%d %H:%M:%S")),
                interval=Interval.MINUTE,
                volume=row["volume"], turnover=row["turnover"],
                open_interest=0,
                open_price=row["open"], high_price=row["high"],
                low_price=row["low"], close_price=row["close"],
            ))
        return bars

    # ========== 回调处理 ==========
    def process_quote(self, data) -> None:
        for _, row in data.iterrows():
            code_str = row["code"]
            date = row["data_date"].replace("-", "")
            t = row["data_time"]
            ts = f"{date} {t}"
            fmt = "%Y%m%d %H:%M:%S.%f" if "." in ts else "%Y%m%d %H:%M:%S"
            dt = datetime.strptime(ts, fmt).replace(tzinfo=CHINA_TZ)
            tick = self.get_tick(code_str)
            tick.datetime = dt
            tick.open_price = row["open_price"]
            tick.high_price = row["high_price"]
            tick.low_price = row["low_price"]
            tick.pre_close = row["prev_close_price"]
            tick.last_price = row["last_price"]
            tick.volume = row["volume"]
            if "price_spread" in row:
                s = row["price_spread"]
                tick.limit_up = tick.last_price + s * 10
                tick.limit_down = tick.last_price - s * 10
            self.on_tick(copy(tick))

    def process_orderbook(self, data) -> None:
        code_str = data["code"]
        tick = self.get_tick(code_str)
        d = tick.__dict__
        bids = data.get("Bid", [])
        asks = data.get("Ask", [])
        for i in range(5):
            n = i + 1
            if i < len(bids) and i < len(asks):
                b, a = bids[i], asks[i]
                d[f"bid_price_{n}"] = b[0]; d[f"bid_volume_{n}"] = b[1]
                d[f"ask_price_{n}"] = a[0]; d[f"ask_volume_{n}"] = a[1]
            else:
                d[f"bid_price_{n}"] = 0.0; d[f"bid_volume_{n}"] = 0
                d[f"ask_price_{n}"] = 0.0; d[f"ask_volume_{n}"] = 0
        if tick.datetime:
            self.on_tick(copy(tick))

    def process_order(self, data) -> None:
        for _, row in data.iterrows():
            if row["order_status"] == OrderStatus.DELETED:
                continue
            direction, offset = DIRECTION_FUTU2VT[row["trd_side"]]
            symbol, exchange = convert_symbol_futu2vt(row["code"])
            orderid = str(row["order_id"])
            order = OrderData(
                symbol=symbol,
                exchange=exchange,
                orderid=orderid,
                direction=direction, offset=offset,
                price=float(row["price"]), volume=int(row["qty"]),
                traded=int(row.get("dealt_qty", 0)),
                status=STATUS_FUTU2VT[row["order_status"]],
                datetime=generate_datetime(row["create_time"]),
                gateway_name=self.gateway_name,
            )
            order.vt_orderid = f"{self.gateway_name}.{orderid}"
            # ★ 写入本地缓存
            self.orders[order.vt_orderid] = order
            self.orders[orderid] = order
            self.on_order(order)

    def process_deal(self, data) -> None:
        for _, row in data.iterrows():
            tid = str(row["deal_id"])
            if tid in self.trades:
                continue
            self.trades.add(tid)
            direction, offset = DIRECTION_FUTU2VT[row["trd_side"]]
            symbol, exchange = convert_symbol_futu2vt(row["code"])
            self.on_trade(TradeData(
                symbol=symbol, exchange=exchange,
                direction=direction, offset=offset,
                tradeid=tid, orderid=str(row["order_id"]),
                price=float(row["price"]), volume=int(row["qty"]),
                datetime=generate_datetime(row["create_time"]),
                gateway_name=self.gateway_name,
            ))


# ========== 工具函数 ==========
def convert_symbol_futu2vt(code) -> tuple:
    parts = str(code).split(".")
    futu_exchange = parts[0]
    futu_symbol = ".".join(parts[1:])
    return futu_symbol, EXCHANGE_FUTU2VT.get(futu_exchange, Exchange.SMART)

def convert_symbol_vt2futu(symbol, exchange) -> str:
    return f"{EXCHANGE_VT2FUTU.get(exchange, 'US')}.{symbol}"

def generate_datetime(s: str) -> datetime:
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(s, fmt).replace(tzinfo=CHINA_TZ)


# ========== FutuDatafeed 占位类 ==========
class FutuDatafeed:
    """富途数据导入（占位类，满足 vnpy_futu.__init__ 导入要求）"""
    def __init__(self):
        self.name = "FutuDatafeed"
