"""
futu_gateway.py — 富途网关 v2.8.2 (FINAL FIX)
修复：
1. EXCHANGE_FUTU2VT 显式硬编码（已解决合约映射）
2. query_contract 使用 SecurityType 枚举（已解决合约注册）
3. 【核心修复】网关自注册到 MainEngine，解决"找不到底层接口：FUTU_US"
4. subscribe 保留动态注册作为防御（不是绕过，是时序保障）
"""

import pandas as pd
from copy import copy
from datetime import datetime
from threading import Thread
from time import sleep
from typing import Any, Dict, List, Set, Union

from futu import (
    ModifyOrderOp, TrdSide, TrdEnv, TrdMarket, KLType,
    OpenQuoteContext, OrderBookHandlerBase, OrderStatus, OrderType as FutuOrderType,
    RET_ERROR, RET_OK, StockQuoteHandlerBase,
    TradeDealHandlerBase, TradeOrderHandlerBase,
    OpenSecTradeContext, OpenFutureTradeContext,
    CurKlineHandlerBase, SubType, SecurityType,
)
try:
    from futu import Session
    SESSION_NONE = Session.NONE
    SESSION_ALL = Session.ALL
except ImportError:
    SESSION_NONE = 0
    SESSION_ALL = 1

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

from .multi_period_kline_handler import MultiPeriodKlineHandler

EVENT_BAR = "eBar"

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

DIRECTION_VT2FUTU: Dict = {Direction.LONG: TrdSide.BUY, Direction.SHORT: TrdSide.SELL}
DIRECTION_FUTU2VT: Dict = {
    TrdSide.BUY: (Direction.LONG, Offset.OPEN),
    TrdSide.SELL: (Direction.SHORT, Offset.OPEN),
    TrdSide.BUY_BACK: (Direction.LONG, Offset.CLOSE),
    TrdSide.SELL_SHORT: (Direction.SHORT, Offset.CLOSE),
}

# ========== 显式硬编码，杜绝反转错误 ==========
EXCHANGE_VT2FUTU: Dict = {
    Exchange.SMART: "US",
    Exchange.SEHK: "HK",
    Exchange.HKFE: "HK_FUTURE",
}
EXCHANGE_FUTU2VT: Dict = {
    "US": Exchange.SMART,
    "HK": Exchange.SEHK,
    "HK_FUTURE": Exchange.HKFE,
}
# =============================================

SEC_TYPE_FUTU2VT = {
    SecurityType.STOCK: Product.EQUITY,
    SecurityType.ETF: Product.ETF,
    SecurityType.IDX: Product.INDEX,
    SecurityType.WARRANT: Product.WARRANT,
    SecurityType.BOND: Product.BOND,
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")


class FutuGateway(BaseGateway):
    default_name = "FUTU"
    default_setting = {"密码": "", "地址": "127.0.0.1", "端口": 11111, "市场": "US", "环境": TrdEnv.SIMULATE}
    exchanges = list(EXCHANGE_FUTU2VT.values())

    def __init__(self, event_engine: EventEngine, gateway_name: str, main_engine=None) -> None:
        super().__init__(event_engine, gateway_name)
        self.main_engine = main_engine  # 保存主引擎引用，用于自注册
        self.quote_ctx: OpenQuoteContext = None
        self.trade_ctx: Union[OpenSecTradeContext, OpenFutureTradeContext] = None
        self.host = ""
        self.port = 0
        self.market = "US"
        self.password = ""
        self.env = TrdEnv.SIMULATE
        self.ticks: Dict[str, TickData] = {}
        self.orders: Dict[str, OrderData] = {}
        self.trades: Set = set()
        self.contracts: Dict[str, ContractData] = {}
        self.thread = Thread(target=self.query_data, daemon=True)
        self.count = 0
        self.interval = 3
        self.query_funcs = [self.query_account, self.query_position]
        self.kline_handler: MultiPeriodKlineHandler = None
        self.market_bus = None
        self.hk_stock_acc_id = 0
        self._registered = False  # 防止重复注册

    def connect(self, setting: dict) -> None:
        self.host = setting.get("地址", "127.0.0.1")
        self.port = int(setting.get("端口", 11111))
        self.market = setting.get("市场", "US")
        self.password = setting.get("密码", "")
        self.env = setting.get("环境", TrdEnv.SIMULATE)
        self.connect_quote()
        self.connect_trade()
        # 【核心修复】自注册到 MainEngine，使 main_engine.gateways["FUTU_US"] 存在
        self._register_to_main_engine()
        self.thread.start()

    def _register_to_main_engine(self) -> None:
        """将本网关实例注册到 MainEngine 的 gateways 字典中"""
        if self._registered:
            return
        if self.main_engine is not None:
            self.main_engine.gateways[self.gateway_name] = self
            self._registered = True
            self.write_log(f"[{self.gateway_name}] 已注册到 MainEngine.gateways['{self.gateway_name}']")
        else:
            self.write_log(f"[{self.gateway_name}] 警告：main_engine 为 None，无法自注册")

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
        self.kline_handler = MultiPeriodKlineHandler(self, market_bus=self.market_bus)
        self.quote_ctx.set_handler(self.kline_handler)
        self.quote_ctx.start()
        self.write_log(f"[{self.gateway_name}] 行情接口连接成功（含多周期K线）")

    def connect_trade(self) -> None:
        if self.market == "HK":
            self.trade_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.HK, host=self.host, port=self.port)
        elif self.market == "US":
            self.trade_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=self.host, port=self.port)
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

        try:
            code, data = self.trade_ctx.unlock_trade(self.password)
            if code == RET_OK:
                self.write_log(f"[{self.gateway_name}] 交易接口解锁成功")
            else:
                self.write_log(f"[{self.gateway_name}] 交易接口解锁提示: {data}")
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 交易解锁接口不可用: {e}")

        if self.market == "HK":
            ret, acc_list = self.trade_ctx.get_acc_list()
            if ret == RET_OK:
                for _, row in acc_list.iterrows():
                    if row.get("sim_acc_type") == "STOCK":
                        self.hk_stock_acc_id = int(row["acc_id"])
                        self.write_log(f"[{self.gateway_name}] 港股股票模拟账号 acc_id={self.hk_stock_acc_id}")
                        break
                if self.hk_stock_acc_id == 0:
                    self.write_log(f"[{self.gateway_name}] 警告：未找到STOCK模拟账号，使用默认acc_id=0")
            else:
                self.write_log(f"[{self.gateway_name}] 查询账户列表失败: {acc_list}")

        self.trade_ctx.set_handler(OrderHandler())
        self.trade_ctx.set_handler(DealHandler())
        self.trade_ctx.start()
        self.write_log(f"[{self.gateway_name}] 交易接口连接成功")

    def subscribe(self, req: SubscribeRequest) -> None:
        try:
            futu_exchange = EXCHANGE_VT2FUTU[req.exchange]
        except KeyError:
            self.write_log(f"不支持的交易所: {req.exchange}")
            return

        vt_symbol = f"{req.symbol}.{req.exchange.value}"

        # 防御性注册：若合约池里没有，动态查询并注册（时序保障，非绕过）
        if vt_symbol not in self.contracts:
            self._register_single_contract(req.symbol, req.exchange, futu_exchange)

        futu_symbol = f"{futu_exchange}.{req.symbol}"
        sub_types = [SubType.QUOTE, SubType.ORDER_BOOK, SubType.K_1M, SubType.K_5M, SubType.K_15M, SubType.K_60M]
        session = SESSION_ALL if futu_exchange == "US" else SESSION_NONE
        code, data = self.quote_ctx.subscribe(futu_symbol, sub_types, session=session)
        if code == RET_OK:
            self.write_log(f"[{self.gateway_name}] ✅ 全套订阅成功: {futu_symbol} (QUOTE+OB+K_1M+5M+15M+60M)")
        else:
            self.write_log(f"[{self.gateway_name}] ❌ 订阅失败: {futu_symbol} | {data}")

    def _register_single_contract(self, symbol: str, exchange: Exchange, futu_exchange: str) -> None:
        vt_symbol = f"{symbol}.{exchange.value}"
        futu_code = f"{futu_exchange}.{symbol}"
        try:
            ret, data = self.quote_ctx.get_stock_basicinfo(
                futu_exchange, SecurityType.STOCK, [futu_code]
            )
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                contract = ContractData(
                    symbol=symbol, exchange=exchange, name=row["name"],
                    product=Product.EQUITY, size=1, pricetick=0.001,
                    history_data=True, net_position=True, gateway_name=self.gateway_name,
                )
                self.on_contract(contract)
                self.contracts[vt_symbol] = contract
                self.write_log(f"[{self.gateway_name}] [动态注册] {vt_symbol} 成功")
            else:
                self.write_log(f"[{self.gateway_name}] [动态注册] {futu_code} 查询失败: {data}")
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] [动态注册] 异常: {e}")

    def send_order(self, req: OrderRequest) -> str:
        side = DIRECTION_VT2FUTU[req.direction]
        adj = 0.05 if req.direction is Direction.LONG else -0.05
        code, data = self.trade_ctx.place_order(req.price, req.volume, req.symbol, side,
                                                FutuOrderType.NORMAL, trd_env=self.env, adjust_limit=adj)
        if code:
            self.write_log(f"[{self.gateway_name}] 委托失败: {data}")
            return ""
        orderid = ""
        for _, row in data.iterrows():
            orderid = str(row["order_id"])
        if not orderid:
            self.write_log(f"[{self.gateway_name}] 下单返回空 orderid")
            return ""
        order = OrderData(symbol=req.symbol, exchange=req.exchange, orderid=orderid,
                          direction=req.direction, offset=req.offset, price=req.price,
                          volume=req.volume, traded=0, status=Status.SUBMITTING,
                          gateway_name=self.gateway_name, datetime=datetime.now(CHINA_TZ))
        order.vt_orderid = f"{self.gateway_name}.{orderid}"
        order.reference = req.reference
        self.orders[order.vt_orderid] = order
        self.orders[orderid] = order
        self.write_log(f"[{self.gateway_name}] 委托成功: {order.vt_orderid} {req.symbol} {req.volume}@{req.price}")
        self.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        code, data = self.trade_ctx.modify_order(ModifyOrderOp.CANCEL, req.orderid, 0, 0, trd_env=self.env)
        if code:
            self.write_log(f"[{self.gateway_name}] 撤单失败: {data}")
        else:
            self.write_log(f"[{self.gateway_name}] 撤单请求已发送: {req.orderid}")

    def query_account(self) -> None:
        if self.market == "HK" and self.hk_stock_acc_id != 0:
            code, data = self.trade_ctx.accinfo_query(trd_env=self.env, acc_id=self.hk_stock_acc_id)
        else:
            code, data = self.trade_ctx.accinfo_query(trd_env=self.env, acc_id=0)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询账户资金失败: {data}")
            return
        for _, row in data.iterrows():
            total_assets = float(row["total_assets"])
            cash = float(row.get("cash", total_assets))
            frozen = total_assets - cash
            self.on_account(AccountData(accountid=f"{self.gateway_name}", balance=total_assets, frozen=frozen,
                                        gateway_name=self.gateway_name))
            self.write_log(f"[{self.gateway_name}] 账户: 总资产=${total_assets:,.2f} 冻结=${frozen:,.2f} 可用=${cash:,.2f}")

    def query_position(self) -> None:
        if self.market == "HK" and self.hk_stock_acc_id != 0:
            code, data = self.trade_ctx.position_list_query(trd_env=self.env, acc_id=self.hk_stock_acc_id)
        else:
            code, data = self.trade_ctx.position_list_query(trd_env=self.env, acc_id=0)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询持仓失败: {data}")
            return
        for _, row in data.iterrows():
            symbol, exchange = convert_symbol_futu2vt(row["code"])
            self.on_position(PositionData(symbol=symbol, exchange=exchange, direction=Direction.NET,
                                          volume=int(row["qty"]),
                                          frozen=float(row["qty"]) - float(row.get("can_sell_qty", row["qty"])),
                                          price=float(row["cost_price"]), pnl=float(row["pl_val"]),
                                          gateway_name=self.gateway_name))

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

    def query_contract(self) -> None:
        market = "HK" if self.market in ["HK", "HK_FUTURE"] else self.market
        count = 0
        for sec_type, vt_product in SEC_TYPE_FUTU2VT.items():
            try:
                code, data = self.quote_ctx.get_stock_basicinfo(market, sec_type)
            except Exception as e:
                self.write_log(f"[{self.gateway_name}] get_stock_basicinfo({market}, {sec_type}) 异常: {e}")
                continue
            if code:
                continue
            if data is None or data.empty:
                continue
            for _, row in data.iterrows():
                symbol, exchange = convert_symbol_futu2vt(row["code"])
                contract = ContractData(
                    symbol=symbol, exchange=exchange, name=row["name"],
                    product=vt_product, size=1, pricetick=0.001,
                    history_data=True, net_position=True, gateway_name=self.gateway_name,
                )
                self.on_contract(contract)
                self.contracts[contract.vt_symbol] = contract
                count += 1
        self.write_log(f"[{self.gateway_name}] 合约查询完成: {count} 个")
        sample = list(self.contracts.keys())[:10]
        self.write_log(f"[{self.gateway_name}] 合约样本: {sample}")

    def close(self) -> None:
        if self.quote_ctx:
            self.quote_ctx.close()
        if self.trade_ctx:
            self.trade_ctx.close()

    def get_tick(self, code_str) -> TickData:
        tick = self.ticks.get(code_str)
        symbol, exchange = convert_symbol_futu2vt(code_str)
        if not tick:
            tick = TickData(symbol=symbol, exchange=exchange, datetime=datetime.now(CHINA_TZ),
                            gateway_name=self.gateway_name)
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
        ret, df, key = self.quote_ctx.request_history_kline(code=futu_symbol, start=start, end=end, ktype=KLType.K_1M)
        if ret != RET_OK:
            self.write_log(f"[{self.gateway_name}] 获取K线失败: {df}")
            return bars
        while key is not None:
            ret, more, key = self.quote_ctx.request_history_kline(code=futu_symbol, start=start, end=end,
                                                                   ktype=KLType.K_1M, page_req_key=key)
            if ret == RET_OK:
                df = pd.concat([df, more], ignore_index=True)
        df["time_key"] = pd.to_datetime(df["time_key"]) - pd.Timedelta(1, "m")
        for _, row in df.iterrows():
            bars.append(BarData(gateway_name=self.gateway_name, symbol=req.symbol, exchange=req.exchange,
                                datetime=generate_datetime(row["time_key"].strftime("%Y-%m-%d %H:%M:%S")),
                                interval=Interval.MINUTE, volume=row["volume"], turnover=row["turnover"],
                                open_interest=0, open_price=row["open"], high_price=row["high"],
                                low_price=row["low"], close_price=row["close"]))
        return bars

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
            tick.low_price = row.get("low_price", row.get("prev_close_price", 0))
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
            order = OrderData(symbol=symbol, exchange=exchange, orderid=orderid, direction=direction, offset=offset,
                              price=float(row["price"]), volume=int(row["qty"]), traded=int(row.get("dealt_qty", 0)),
                              status=STATUS_FUTU2VT[row["order_status"]],
                              datetime=generate_datetime(row["create_time"]), gateway_name=self.gateway_name)
            order.vt_orderid = f"{self.gateway_name}.{orderid}"
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
            self.on_trade(TradeData(symbol=symbol, exchange=exchange, direction=direction, offset=offset,
                                    tradeid=tid, orderid=str(row["order_id"]), price=float(row["price"]),
                                    volume=int(row["qty"]), datetime=generate_datetime(row["create_time"]),
                                    gateway_name=self.gateway_name))


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


class FutuDatafeed:
    def __init__(self):
        self.name = "FutuDatafeed"