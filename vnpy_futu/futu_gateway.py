"""
futu_gateway.py — 富途网关 v2.9.3 (FINAL)
修复记录：
1. connect_trade: 精确选择模拟账户（港股优先 MARGIN，美股 STOCK_AND_OPTION）
2. query_account: 按富途官方文档取值，富途字段无效时自动推算
   - 港股: hkd_net_cash_power → power → 自动推算（MARGIN×2，CASH×1）
   - 美股: usd_net_cash_power → power → us_cash
3. send_order: 增加资金预检，调用富途「查询最大可买可卖」接口
4. 所有 float() 改为 _safe_float，防御 'N/A' 崩溃
"""
import pandas as pd
from copy import copy
from datetime import datetime
from threading import Thread
from time import sleep
from typing import Any, Dict, List, Set, Union, Optional

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

from vnpy.event import EventEngine, Event
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

# ========== 显式硬编码 ==========
EXCHANGE_VT2FUTU: Dict = {
    Exchange.SMART: "US",
    Exchange.SEHK: "HK",
    "HK_FUTURE": "HK_FUTURE",
}
EXCHANGE_FUTU2VT: Dict = {
    "US": Exchange.SMART,
    "HK": Exchange.SEHK,
    "HK_FUTURE": Exchange.HKFE,
}
# ==================================

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
        self.main_engine = main_engine
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
        self.acc_id = 0
        self.acc_type = "MARGIN"
        self.acc_info: Dict[str, Any] = {}
        self._registered = False
        self._max_trd_qty_cache: Dict[str, tuple] = {}
        self._max_trd_qty_cache_ttl = 30

    # ───────────────────────────────────────
    #  安全浮点转换辅助方法
    # ───────────────────────────────────────
    @staticmethod
    def _safe_float(val, default=0.0):
        if val is None:
            return default
        if isinstance(val, (int, float)):
            try:
                return float(val)
            except (ValueError, OverflowError):
                return default
        if isinstance(val, str):
            s = val.strip()
            if s == '' or s.upper() == 'N/A':
                return default
            try:
                return float(s)
            except ValueError:
                return default
        return default

    # ══════════════════════════════════════
    #  连接主流程
    # ══════════════════════════════════════
    def connect(self, setting: dict) -> None:
        self.host = setting.get("地址", "127.0.0.1")
        self.port = int(setting.get("端口", 11111))
        self.market = setting.get("市场", "US")
        self.password = setting.get("密码", "")
        env_val = setting.get("环境", TrdEnv.SIMULATE)
        if isinstance(env_val, str):
            self.env = getattr(TrdEnv, env_val, TrdEnv.SIMULATE)
        else:
            self.env = env_val
        self.connect_quote()
        self.connect_trade()
        self._register_to_main_engine()
        self.thread.start()

    def _register_to_main_engine(self) -> None:
        if self._registered:
            return
        if self.main_engine is not None:
            self.main_engine.gateways[self.gateway_name] = self
            self._registered = True
            self.write_log(f"[{self.gateway_name}] 已注册到 MainEngine.gateways['{self.gateway_name}']")
        else:
            self.write_log(f"[{self.gateway_name}] 警告：main_engine 为 None，无法自注册")

    # ══════════════════════════════════════
    #  行情连接
    # ══════════════════════════════════════
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

    # ══════════════════════════════════════
    #  ★ 交易连接 + 精确选择模拟账户 ★
    # ══════════════════════════════════════
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

        self.acc_id = 0
        ret, acc_list = self.trade_ctx.get_acc_list()
        if ret == RET_OK and not acc_list.empty:
            self.write_log(f"[{self.gateway_name}] 账户列表 (共 {len(acc_list)} 个):")
            for _, row in acc_list.iterrows():
                self.write_log(
                    f"  acc_id={row['acc_id']} env={row['trd_env']} "
                    f"type={row['acc_type']} sim_type={row.get('sim_acc_type', 'N/A')} "
                    f"status={row.get('acc_status', 'N/A')}"
                )

            sim_accounts = acc_list[acc_list['trd_env'] == 'SIMULATE']

            if self.market == "US":
                us_sim = sim_accounts[sim_accounts['sim_acc_type'] == 'STOCK_AND_OPTION']
                if not us_sim.empty:
                    self.acc_id = int(us_sim.iloc[0]['acc_id'])
                    self.acc_type = str(us_sim.iloc[0].get('acc_type', 'MARGIN'))
                    self.write_log(f"[{self.gateway_name}] ✅ 选中: 美股模拟(股票+期权) acc_id={self.acc_id}")
                elif not sim_accounts.empty:
                    self.acc_id = int(sim_accounts.iloc[0]['acc_id'])
                    self.acc_type = str(sim_accounts.iloc[0].get('acc_type', 'MARGIN'))
                    self.write_log(f"[{self.gateway_name}] ⚠️ 未找到 STOCK_AND_OPTION，使用首个模拟账号 acc_id={self.acc_id}")
            elif self.market == "HK":
                # 港股：优先选 MARGIN(融资) 模拟账户，其次 CASH(现金)
                hk_margin = sim_accounts[
                    (sim_accounts['sim_acc_type'] == 'STOCK') &
                    (sim_accounts['acc_type'] == 'MARGIN')
                ]
                hk_cash = sim_accounts[
                    (sim_accounts['sim_acc_type'] == 'STOCK') &
                    (sim_accounts['acc_type'] == 'CASH')
                ]
                if not hk_margin.empty:
                    self.acc_id = int(hk_margin.iloc[0]['acc_id'])
                    self.acc_type = "MARGIN"
                    self.write_log(f"[{self.gateway_name}] ✅ 选中: 港股融资模拟(股票) acc_id={self.acc_id}")
                elif not hk_cash.empty:
                    self.acc_id = int(hk_cash.iloc[0]['acc_id'])
                    self.acc_type = "CASH"
                    self.write_log(f"[{self.gateway_name}] ⚠️ 未找到融资模拟，降级使用现金模拟 acc_id={self.acc_id}")
                elif not sim_accounts.empty:
                    self.acc_id = int(sim_accounts.iloc[0]['acc_id'])
                    self.acc_type = str(sim_accounts.iloc[0].get('acc_type', 'CASH'))
                    self.write_log(f"[{self.gateway_name}] ⚠️ 兜底使用首个模拟账号 acc_id={self.acc_id} type={self.acc_type}")

            if self.acc_id == 0:
                self.write_log(f"[{self.gateway_name}] ⚠️ 警告：未找到模拟账号！acc_id 保持 0")
        else:
            self.write_log(f"[{self.gateway_name}] ⚠️ get_acc_list 失败: {acc_list}")

        self.trade_ctx.set_handler(OrderHandler())
        self.trade_ctx.set_handler(DealHandler())
        self.trade_ctx.start()
        self.write_log(f"[{self.gateway_name}] 交易接口连接成功 (最终 acc_id={self.acc_id})")

    # ══════════════════════════════════════
    #  订阅
    # ══════════════════════════════════════
    def subscribe(self, req: SubscribeRequest) -> None:
        try:
            futu_exchange = EXCHANGE_VT2FUTU[req.exchange]
        except KeyError:
            self.write_log(f"不支持的交易所: {req.exchange}")
            return

        vt_symbol = f"{req.symbol}.{req.exchange.value}"

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
            ret, data = self.quote_ctx.get_stock_basicinfo(futu_exchange, SecurityType.STOCK, [futu_code])
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

    # ══════════════════════════════════════
    #  ★ 查询最大可买可卖（富途官方接口）
    # ══════════════════════════════════════
    def query_max_trd_qty(self, futu_symbol: str, price: float) -> Dict[str, int]:
        now = datetime.now().timestamp()
        if futu_symbol in self._max_trd_qty_cache:
            ts, cash_buy, cash_margin_buy = self._max_trd_qty_cache[futu_symbol]
            if now - ts < self._max_trd_qty_cache_ttl:
                return {"max_cash_buy": cash_buy, "max_cash_and_margin_buy": cash_margin_buy}

        try:
            code, data = self.trade_ctx.acctradinginfo_query(
                order_type=FutuOrderType.NORMAL,
                code=futu_symbol,
                price=price,
                trd_env=self.env,
                acc_id=self.acc_id,
            )
            if code == RET_OK and not data.empty:
                cash_buy = int(self._safe_float(data.iloc[0].get("max_cash_buy"), 0))
                cash_margin_buy = int(self._safe_float(data.iloc[0].get("max_cash_and_margin_buy"), 0))
                self._max_trd_qty_cache[futu_symbol] = (now, cash_buy, cash_margin_buy)
                return {"max_cash_buy": cash_buy, "max_cash_and_margin_buy": cash_margin_buy}
            else:
                self.write_log(f"[{self.gateway_name}] 查询最大可买失败 {futu_symbol}: {data}")
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 查询最大可买异常 {futu_symbol}: {e}")
        return {"max_cash_buy": 0, "max_cash_and_margin_buy": 0}

    # ══════════════════════════════════════
    #  ★ 下单 + 资金预检 ★
    # ══════════════════════════════════════
    def send_order(self, req: OrderRequest) -> str:
        side = DIRECTION_VT2FUTU[req.direction]
        futu_symbol = convert_symbol_vt2futu(req.symbol, req.exchange)
        is_buy = req.direction is Direction.LONG

        if is_buy and req.price > 0:
            max_qty_info = self.query_max_trd_qty(futu_symbol, req.price)
            if self.acc_type.upper() == "MARGIN":
                max_allowed = max_qty_info["max_cash_and_margin_buy"]
                limit_label = "融资最大可买"
            else:
                max_allowed = max_qty_info["max_cash_buy"]
                limit_label = "现金最大可买"

            if max_allowed > 0 and req.volume > max_allowed:
                self.write_log(
                    f"[{self.gateway_name}] ⚠️ 资金预检: 请求买入 {req.volume} 股 {futu_symbol} "
                    f"超过{limit_label} {max_allowed} 股，自动缩减"
                )
                req.volume = max_allowed
            elif max_allowed == 0:
                info = self.acc_info
                if info and info.get("power", 0) > 0:
                    est_max = int(info["power"] * 0.95 / req.price)
                    if req.volume > est_max > 0:
                        self.write_log(
                            f"[{self.gateway_name}] ⚠️ 资金预检(估算): 请求 {req.volume} 股 "
                            f"超过估算上限 {est_max} 股，自动缩减"
                        )
                        req.volume = est_max

        if req.volume <= 0:
            self.write_log(f"[{self.gateway_name}] ❌ 资金不足，无法下单 {futu_symbol}")
            return ""

        adj = 0.05 if is_buy else -0.05
        code, data = self.trade_ctx.place_order(
            req.price, req.volume, futu_symbol, side,
            FutuOrderType.NORMAL, trd_env=self.env, acc_id=self.acc_id, adjust_limit=adj
        )
        if code:
            self.write_log(f"[{self.gateway_name}] 委托失败: {data}")
            return ""
        orderid = ""
        for _, row in data.iterrows():
            orderid = str(row["order_id"])
        if not orderid:
            self.write_log(f"[{self.gateway_name}] 下单返回空 orderid")
            return ""
        order = OrderData(
            symbol=req.symbol, exchange=req.exchange, orderid=orderid,
            direction=req.direction, offset=req.offset, price=req.price,
            volume=req.volume, traded=0, status=Status.SUBMITTING,
            gateway_name=self.gateway_name, datetime=datetime.now(CHINA_TZ)
        )
        order.vt_orderid = f"{self.gateway_name}.{orderid}"
        order.reference = req.reference
        self.orders[order.vt_orderid] = order
        self.orders[orderid] = order
        self.write_log(f"[{self.gateway_name}] 委托成功: {order.vt_orderid} {futu_symbol} {req.volume}@{req.price}")
        self.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        code, data = self.trade_ctx.modify_order(
            ModifyOrderOp.CANCEL, req.orderid, 0, 0, trd_env=self.env, acc_id=self.acc_id
        )
        if code:
            self.write_log(f"[{self.gateway_name}] 撤单失败: {data}")
        else:
            self.write_log(f"[{self.gateway_name}] 撤单请求已发送: {req.orderid}")

    # ══════════════════════════════════════
    #  定时查询
    # ══════════════════════════════════════
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

    # ══════════════════════════════════════
    #  ★ 查询账户资金 ★
    # ══════════════════════════════════════
    def query_account(self) -> None:
        if self.acc_id == 0:
            self.write_log(f"[{self.gateway_name}] ⚠️ acc_id 为 0，跳过资金查询")
            return

        try:
            code, data = self.trade_ctx.accinfo_query(trd_env=self.env, acc_id=self.acc_id)
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 查询账户资金异常: {e}")
            return

        if code:
            self.write_log(f"[{self.gateway_name}] 查询账户资金失败: {data}")
            return

        for _, row in data.iterrows():
            total_assets = self._safe_float(row.get("total_assets"), 0.0)
            cash         = self._safe_float(row.get("cash"), total_assets)
            market_val   = self._safe_float(row.get("market_val"), 0.0)
            frozen_cash  = self._safe_float(row.get("frozen_cash"), 0.0)
            power        = self._safe_float(row.get("power"), 0.0)
            avl_cash     = self._safe_float(row.get("avl_withdrawal_cash"), cash)

            if self.market == "US":
                spec_cash  = self._safe_float(row.get("us_cash"), cash)
                spec_power = self._safe_float(
                    row.get("usd_net_cash_power"),
                    self._safe_float(row.get("power"), spec_cash)
                )
                currency = "USD"
            else:  # HK
                spec_cash  = self._safe_float(row.get("hk_cash"), cash)
                real_hkd = self._safe_float(row.get("hkd_net_cash_power"), None)
                real_pow  = self._safe_float(row.get("power"), None)
                if real_hkd is not None and real_hkd > 0:
                    spec_power = real_hkd
                elif real_pow is not None and real_pow > 0:
                    spec_power = real_pow
                else:
                    # 富途无有效值 → 按账户类型推算
                    if self.acc_type.upper() == "MARGIN":
                        spec_power = spec_cash * 2.0
                    else:
                        spec_power = spec_cash
                currency = "HKD"

            # 调试日志（首次打印完整字段）
            if not hasattr(self, '_debug_dumped'):
                self._debug_dumped = True
                self.write_log(f"[{self.gateway_name}] 调试 accinfo 字段: {list(data.columns)}")
                self.write_log(f"[{self.gateway_name}] 调试 accinfo 首行: {dict(row)}")

            self.acc_info = {
                "gateway": self.gateway_name,
                "acc_id": self.acc_id,
                "total_assets": total_assets,
                "cash": spec_cash,
                "raw_cash": cash,
                "market_val": market_val,
                "frozen_cash": frozen_cash,
                "power": spec_power,
                "raw_power": power,
                "avl_withdrawal_cash": avl_cash,
                "currency": currency,
                "market": self.market,
                "acc_type": self.acc_type,
            }

            self.on_account(AccountData(
                accountid=f"{self.gateway_name}_{self.acc_id}",
                balance=total_assets,
                frozen=frozen_cash,
                gateway_name=self.gateway_name
            ))

            self.write_log(
                f"[{self.gateway_name}] 账户: 总资产=${total_assets:,.2f} "
                f"现金=${spec_cash:,.2f} 证券市值=${market_val:,.2f} "
                f"冻结=${frozen_cash:,.2f} 购买力=${spec_power:,.2f} ({currency})"
            )

    def query_position(self) -> None:
        if self.acc_id == 0:
            return
        try:
            code, data = self.trade_ctx.position_list_query(trd_env=self.env, acc_id=self.acc_id)
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 查询持仓异常: {e}")
            return
        if code:
            self.write_log(f"[{self.gateway_name}] 查询持仓失败: {data}")
            return
        for _, row in data.iterrows():
            symbol, exchange = convert_symbol_futu2vt(row["code"])
            qty = self._safe_float(row.get("qty"), 0.0)
            can_sell = self._safe_float(row.get("can_sell_qty", qty), qty)
            self.on_position(PositionData(
                symbol=symbol, exchange=exchange, direction=Direction.NET,
                volume=int(qty),
                frozen=qty - can_sell,
                price=self._safe_float(row.get("cost_price"), 0.0),
                pnl=self._safe_float(row.get("pl_val"), 0.0),
                gateway_name=self.gateway_name
            ))

    def query_order(self) -> None:
        if self.acc_id == 0:
            return
        code, data = self.trade_ctx.order_list_query("", trd_env=self.env, acc_id=self.acc_id)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询委托失败: {data}")
            return
        self.process_order(data)

    def query_trade(self) -> None:
        if self.acc_id == 0:
            return
        code, data = self.trade_ctx.deal_list_query("", trd_env=self.env, acc_id=self.acc_id)
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
        self.event_engine.put(Event("eContractReady", self.gateway_name))

    # ══════════════════════════════════════
    #  关闭 / 工具
    # ══════════════════════════════════════
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
            ret, more, key = self.quote_ctx.request_history_kline(
                code=futu_symbol, start=start, end=end, ktype=KLType.K_1M, page_req_key=key
            )
            if ret == RET_OK:
                df = pd.concat([df, more], ignore_index=True)
        df["time_key"] = pd.to_datetime(df["time_key"]) - pd.Timedelta(1, "m")
        for _, row in df.iterrows():
            bars.append(BarData(
                gateway_name=self.gateway_name, symbol=req.symbol, exchange=req.exchange,
                datetime=generate_datetime(row["time_key"].strftime("%Y-%m-%d %H:%M:%S")),
                interval=Interval.MINUTE, volume=row["volume"], turnover=row["turnover"],
                open_interest=0, open_price=row["open"], high_price=row["high"],
                low_price=row["low"], close_price=row["close"]
            ))
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
            order = OrderData(
                symbol=symbol, exchange=exchange, orderid=orderid,
                direction=direction, offset=offset,
                price=self._safe_float(row.get("price"), 0.0),
                volume=int(self._safe_float(row.get("qty"), 0.0)),
                traded=int(self._safe_float(row.get("dealt_qty"), 0.0)),
                status=STATUS_FUTU2VT[row["order_status"]],
                datetime=generate_datetime(row["create_time"]),
                gateway_name=self.gateway_name
            )
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
            self.on_trade(TradeData(
                symbol=symbol, exchange=exchange, direction=direction, offset=offset,
                tradeid=tid, orderid=str(row["order_id"]),
                price=self._safe_float(row.get("price"), 0.0),
                volume=int(self._safe_float(row.get("qty"), 0.0)),
                datetime=generate_datetime(row["create_time"]),
                gateway_name=self.gateway_name
            ))


# ══════════════════════════════════════
#  工具函数
# ══════════════════════════════════════
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