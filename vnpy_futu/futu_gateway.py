"""
futu_gateway.py — 富途网关 v3.0.1
v3.0.1 修复：
  - subscribe() 美股 K_1M 改用 SESSION_NONE：
    只在交易时段(09:30-16:00 美东)推送，避免盘前盘后稀疏报价
    污染 ArrayManager，导致均线/ADX 算出垃圾值。
  - 其余逻辑（按需订阅、Tick 引用计数、落盘、close 等）原样保留。
"""
import sqlite3
import os
import json
import importlib
import numpy as np
import pandas as pd
from copy import copy
from datetime import datetime
from threading import Thread, Lock
from time import sleep
from typing import Any, Dict, List, Set, Union, Optional

from futu import (
    ModifyOrderOp, TrdSide, TrdEnv, TrdMarket, KLType,
    OpenQuoteContext, OrderBookHandlerBase, OrderStatus, OrderType as FutuOrderType,
    RET_ERROR, RET_OK, StockQuoteHandlerBase,
    TradeDealHandlerBase, TradeOrderHandlerBase,
    OpenSecTradeContext, OpenFutureTradeContext,
    TickerHandlerBase,
    SubType, SecurityType, SecurityFirm,
)
try:
    from futu import Session
    SESSION_NONE = Session.NONE
    SESSION_ALL  = Session.ALL
except ImportError:
    SESSION_NONE = 0
    SESSION_ALL  = 1

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
from vnpy.trader.event import EVENT_TIMER, EVENT_TICK
from vnpy.trader.utility import ZoneInfo

# 兼容旧版 vnpy 没有 ZoneInfo 的情况
try:
    from vnpy.trader.utility import get_zone_info as _get_zi
    CHINA_TZ = _get_zi("Asia/Shanghai")
except Exception:
    CHINA_TZ = ZoneInfo("Asia/Shanghai")

# 尝试导入 K 线处理器
try:
    from .multi_period_kline_handler import MultiPeriodKlineHandler
    _HAS_KLINE_HANDLER = True
except ImportError:
    try:
        from vnpy_futu.multi_period_kline_handler import MultiPeriodKlineHandler
        _HAS_KLINE_HANDLER = True
    except ImportError:
        _HAS_KLINE_HANDLER = False

EVENT_BAR = "eBar"

# ══════════════════════════════════
#  工具函数
# ══════════════════════════════════
def convert_symbol_futu2vt(code) -> tuple:
    """富途代码 → (symbol, exchange)。"""
    parts = str(code).split(".")
    if len(parts) >= 2:
        futu_exchange = parts[0]
        futu_symbol = ".".join(parts[1:])
    else:
        futu_exchange = "US"
        futu_symbol = parts[0]
    exchange_map = {
        "US": Exchange.SMART,
        "HK": Exchange.SEHK,
        "HK_FUTURE": Exchange.HKFE,
    }
    return futu_symbol, exchange_map.get(futu_exchange, Exchange.SMART)


def convert_symbol_vt2futu(symbol, exchange) -> str:
    rev = {
        Exchange.SMART: "US",
        Exchange.SEHK:  "HK",
        Exchange.HKFE:  "HK_FUTURE",
    }
    return f"{rev.get(exchange, 'US')}.{symbol}"


def generate_datetime(s: str) -> datetime:
    if not s or s == "0":
        return datetime.now(CHINA_TZ)
    if "." in s:
        fmt = "%Y-%m-%d %H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=CHINA_TZ)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y%m%d %H:%M:%S").replace(tzinfo=CHINA_TZ)
        except ValueError:
            return datetime.now(CHINA_TZ)


def _to_native(v):
    """把 numpy / pandas 标量转成 Python 原生类型，None→0。"""
    if v is None:
        return 0
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.item() if v.size == 1 else v.tolist()
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "__float__") and not isinstance(v, (str, bytes)):
        try:
            return float(v)
        except Exception:
            pass
    return v


# ══════════════════════════════════
#  常 / 枚举映射
# ══════════════════════════════════
STATUS_FUTU2VT: Dict = {
    OrderStatus.NONE:           Status.SUBMITTING,
    OrderStatus.WAITING_SUBMIT: Status.SUBMITTING,
    OrderStatus.SUBMITTING:     Status.SUBMITTING,
    OrderStatus.SUBMITTED:      Status.NOTTRADED,
    OrderStatus.FILLED_PART:    Status.PARTTRADED,
    OrderStatus.FILLED_ALL:     Status.ALLTRADED,
    OrderStatus.CANCELLED_PART: Status.CANCELLED,
    OrderStatus.CANCELLED_ALL:  Status.CANCELLED,
    OrderStatus.FAILED:         Status.REJECTED,
    OrderStatus.DISABLED:       Status.CANCELLED,
    OrderStatus.DELETED:        Status.CANCELLED,
}

DIRECTION_VT2FUTU: Dict = {Direction.LONG: TrdSide.BUY, Direction.SHORT: TrdSide.SELL}
DIRECTION_FUTU2VT: Dict = {
    TrdSide.BUY:         (Direction.LONG,  Offset.OPEN),
    TrdSide.SELL:        (Direction.SHORT, Offset.OPEN),
    TrdSide.BUY_BACK:    (Direction.LONG,  Offset.CLOSE),
    TrdSide.SELL_SHORT:  (Direction.SHORT, Offset.CLOSE),
}

EXCHANGE_VT2FUTU: Dict = {
    Exchange.SMART: "US",
    Exchange.SEHK:  "HK",
    Exchange.HKFE:  "HK_FUTURE",
}
EXCHANGE_FUTU2VT: Dict = {
    "US":       Exchange.SMART,
    "HK":       Exchange.SEHK,
    "HK_FUTURE": Exchange.HKFE,
}

SEC_TYPE_FUTU2VT = {
    SecurityType.STOCK:   Product.EQUITY,
    SecurityType.ETF:    Product.ETF,
    SecurityType.IDX:    Product.INDEX,
    SecurityType.WARRANT: Product.WARRANT,
    SecurityType.BOND:    Product.BOND,
}

CHINA_TZ = ZoneInfo("Asia/Shanghai")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FutuGateway(BaseGateway):
    default_name = "FUTU"
    default_setting = {"密码": "", "地址": "127.0.0.1", "端口": 11111, "市场": "US", "环境": TrdEnv.SIMULATE}
    exchanges = [Exchange.SMART, Exchange.SEHK, Exchange.HKFE]

    # ────────────────────────────────
    #  构造
    # ────────────────────────────────
    def __init__(self, event_engine: EventEngine, gateway_name: str, main_engine=None) -> None:
        super().__init__(event_engine, gateway_name)
        self.main_engine = main_engine
        self.quote_ctx: Optional[OpenQuoteContext] = None
        self.trade_ctx: Optional[Union[OpenSecTradeContext, OpenFutureTradeContext]] = None
        self.host = "127.0.0.1"
        self.port = 11111
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
        self.query_funcs = []

        self.kline_handler = None
        self.market_bus = None
        self.acc_id = 0
        self.acc_type = "MARGIN"
        self.acc_info: Dict[str, Any] = {}
        self._registered = False
        self._max_trd_qty_cache: Dict[str, tuple] = {}
        self._max_trd_qty_cache_ttl = 30

        # Tick 订阅引用计数
        self.tick_ref_count: Dict[str, int] = {}

        # 落盘
        self._tick_db_path = os.path.join(_PROJECT_ROOT, "data", "history.db")
        self._tick_db_conn: Optional[sqlite3.Connection] = None
        self._tick_count = 0
        self._db_ready = False
        self._db_error_count = 0
        self._db_lock = Lock()
        self._debug_dumped = False

    # ────────────────────────────────
    #  类型安全转换
    # ────────────────────────────────
    @staticmethod
    def _safe_float(val, default=0.0):
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError, OverflowError):
            return default

    @staticmethod
    def _safe_int(val, default=0) -> int:
        try:
            return int(FutuGateway._safe_float(val, default))
        except (ValueError, OverflowError):
            return default

    # ══════════════════════════════════
    #  连接主流程
    # ══════════════════════════════════
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

        if self.market == "US":
            self._init_tick_db()
            self.event_engine.register(EVENT_TICK, self._on_event_tick)
            self.write_log(f"[{self.gateway_name}] ✅ Tick 落盘监听器已注册 (EVENT_TICK)")

        self.query_funcs = [self.query_account, self.query_position]
        self.thread.start()

    def _register_to_main_engine(self) -> None:
        if self._registered:
            return
        me = self.main_engine
        if me is None:
            try:
                me = getattr(self.event_engine, 'main_engine', None)
            except Exception:
                me = None
        if me is not None:
            me.gateways[self.gateway_name] = self
            self._registered = True
            self.write_log(f"[{self.gateway_name}] 已注册到 MainEngine.gateways['{self.gateway_name}']")
        else:
            self.write_log(f"[{self.gateway_name}] 警告：main_engine 为 None，无法自注册")

    # ────────────────────────────────
    #  数据库初始化
    # ────────────────────────────────
    def _init_tick_db(self) -> None:
        with self._db_lock:
            try:
                db_dir = os.path.dirname(self._tick_db_path)
                if db_dir and not os.path.exists(db_dir):
                    os.makedirs(db_dir, exist_ok=True)
                if self._tick_db_conn:
                    try:
                        self._tick_db_conn.close()
                    except Exception:
                        pass
                    self._tick_db_conn = None

                for ext in ['-wal', '-shm']:
                    lf = self._tick_db_path + ext
                    if os.path.exists(lf):
                        try:
                            os.remove(lf)
                        except PermissionError:
                            pass

                print(f"[DB INIT] 数据库路径: {os.path.abspath(self._tick_db_path)}", flush=True)
                self._tick_db_conn = sqlite3.connect(
                    self._tick_db_path, check_same_thread=False, isolation_level=None
                )
                cur = self._tick_db_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tick_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        exchange TEXT,
                        datetime TEXT NOT NULL,
                        gateway_name TEXT,
                        name TEXT,
                        last_price REAL,
                        volume REAL,
                        turnover REAL,
                        open_price REAL,
                        high_price REAL,
                        low_price REAL,
                        pre_close REAL,
                        bid_price_1 REAL,
                        ask_price_1 REAL,
                        bid_volume_1 INTEGER,
                        ask_volume_1 INTEGER,
                        source TEXT,
                        saved_at_utc TEXT,
                        received_at TEXT
                    )
                """)
                try:
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tick_unique "
                        "ON tick_data(symbol, exchange, datetime, source)"
                    )
                except Exception:
                    pass

                existing = set(r[1] for r in cur.execute("PRAGMA table_info(tick_data)"))
                for col, ctype in [
                    ("name","TEXT"),("gateway_name","TEXT"),("turnover","REAL"),
                    ("open_price","REAL"),("high_price","REAL"),("low_price","REAL"),
                    ("pre_close","REAL"),("bid_price_1","REAL"),("ask_price_1","REAL"),
                    ("bid_volume_1","INTEGER"),("ask_volume_1","INTEGER"),
                    ("source","TEXT"),("saved_at_utc","TEXT"),("received_at","TEXT"),
                ]:
                    if col not in existing:
                        try:
                            cur.execute(f"ALTER TABLE tick_data ADD COLUMN {col} {ctype}")
                            print(f"[DB INIT] ✅ 补充列: {col}", flush=True)
                        except Exception:
                            pass

                self._tick_db_conn.commit()
                self._db_ready = True
                cols = [r[1] for r in cur.execute("PRAGMA table_info(tick_data)")]
                print(f"[DB INIT] tick_data 列: {cols}", flush=True)
                print(f"[DB INIT] ✅ tick_data 表就绪 | WAL模式", flush=True)
            except Exception as e:
                print(f"[DB INIT ERROR] {e}", flush=True)
                import traceback; traceback.print_exc()
                self._tick_db_conn = None
                self._db_ready = False

    # ────────────────────────────────
    #  判断是否应该落盘（按需）
    # ────────────────────────────────
    def _should_save_tick(self, vt_symbol: str) -> bool:
        futu_symbol = self._vt_to_futu_symbol(vt_symbol)
        return self.tick_ref_count.get(futu_symbol, 0) > 0

    # ────────────────────────────────
    #  Tick 落盘（完全容错，按需执行）
    # ────────────────────────────────
    def _save_tick_to_db(self, tick: TickData, source: str = 'quote') -> None:
        if not self._db_ready or self._tick_db_conn is None:
            return
        if not self._should_save_tick(tick.vt_symbol):
            return
        with self._db_lock:
            try:
                self._tick_db_conn.execute("SELECT 1")
            except Exception:
                self.write_log(f"[{self.gateway_name}] DB 断开，重连...")
                self._init_tick_db()
                if not self._db_ready:
                    return

            try:
                if tick.datetime and getattr(tick.datetime, 'tzinfo', None):
                    ts_utc = tick.datetime.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S.%f")
                else:
                    ts_utc = (tick.datetime or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S.%f")
                saved_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
                received_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

                sql = """
                    INSERT OR IGNORE INTO tick_data (
                        symbol, exchange, datetime, gateway_name, name,
                        last_price, volume, turnover,
                        open_price, high_price, low_price, pre_close,
                        bid_price_1, ask_price_1, bid_volume_1, ask_volume_1,
                        source, saved_at_utc, received_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """
                raw = [
                    str(getattr(tick, 'symbol', '') or ''),
                    str(getattr(tick, 'exchange', '') or ''),
                    ts_utc,
                    str(self.gateway_name or ''),
                    str(getattr(tick, 'name', '') or ''),
                    self._safe_float(getattr(tick, 'last_price', 0)),
                    self._safe_float(getattr(tick, 'volume', 0)),
                    self._safe_float(getattr(tick, 'turnover', 0)),
                    self._safe_float(getattr(tick, 'open_price', 0)),
                    self._safe_float(getattr(tick, 'high_price', 0)),
                    self._safe_float(getattr(tick, 'low_price', 0)),
                    self._safe_float(getattr(tick, 'pre_close', 0)),
                    self._safe_float(getattr(tick, 'bid_price_1', 0)),
                    self._safe_float(getattr(tick, 'ask_price_1', 0)),
                    self._safe_int(getattr(tick, 'bid_volume_1', 0)),
                    self._safe_int(getattr(tick, 'ask_volume_1', 0)),
                    str(source or ''),
                    saved_at,
                    received_at,
                ]
                params = tuple(_to_native(v) for v in raw)
                self._tick_db_conn.execute(sql, params)
                self._tick_count += 1
                self._db_error_count = 0

                if self._tick_count % 100 == 0:
                    self._tick_db_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    self._tick_db_conn.commit()
                    self.write_log(f"[DB CHECKPOINT] {self.gateway_name} @ {self._tick_count}")
                else:
                    self._tick_db_conn.commit()
            except Exception as e:
                self._db_error_count += 1
                if self._db_error_count <= 3 or self._db_error_count % 200 == 0:
                    self.write_log(
                        f"[DB ERROR] {self.gateway_name} {source}: {e} | "
                        f"cnt={self._db_error_count}"
                    )
                try:
                    self._tick_db_conn.close()
                except Exception:
                    pass
                self._tick_db_conn = None
                self._db_ready = False

    def _on_event_tick(self, event: Event) -> None:
        if self.market != "US":
            return
        tick = event.data
        if not tick:
            return
        if getattr(tick, 'gateway_name', '') != self.gateway_name:
            return
        if self._should_save_tick(tick.vt_symbol):
            self._save_tick_to_db(tick, source='event_tick')

    # ══════════════════════════════════
    #  行情连接
    # ══════════════════════════════════
    def connect_quote(self) -> None:
        try:
            self.quote_ctx = OpenQuoteContext(self.host, self.port)
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 行情连接异常: {e}")
            return

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

        self.quote_ctx.set_handler(QuoteHandler())
        self.quote_ctx.set_handler(OrderBookHandler())
        self.quote_ctx.set_handler(TickerHandler())

        if _HAS_KLINE_HANDLER:
            try:
                self.kline_handler = MultiPeriodKlineHandler(self, market_bus=self.market_bus)
                self.quote_ctx.set_handler(self.kline_handler)
            except Exception as e:
                self.write_log(f"[{self.gateway_name}] K线处理器初始化失败(非致命): {e}")
                self.kline_handler = None

        self.quote_ctx.start()
        self.write_log(f"[{self.gateway_name}] 行情接口连接成功（含多周期K线+TICKER Handler）")

    # ══════════════════════════════════
    #  交易连接
    # ══════════════════════════════════
    def connect_trade(self) -> None:
        try:
            if self.market == "HK":
                self.trade_ctx = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.HK,
                    host=self.host, port=self.port,
                    security_firm=SecurityFirm.FUTUSECURITIES,
                )
            elif self.market == "US":
                self.trade_ctx = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.US,
                    host=self.host, port=self.port,
                    security_firm=SecurityFirm.FUTUSECURITIES,
                )
            elif self.market == "HK_FUTURE":
                self.trade_ctx = OpenFutureTradeContext(host=self.host, port=self.port)
            else:
                self.write_log(f"[{self.gateway_name}] 未知市场: {self.market}")
                return
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 交易连接异常: {e}")
            return

        class OrderHandler(TradeOrderHandlerBase):
            gateway = self
            def on_recv_rsp(self, rsp_str):
                ret_code, content = super().on_recv_rsp(rsp_str)
                if ret_code != RET_OK:
                    return RET_ERROR, content
                try:
                    self.gateway.process_order(content)
                except Exception as e:
                    self.gateway.write_log(f"[OrderHandler] error: {e}")
                return RET_OK, content

        class DealHandler(TradeDealHandlerBase):
            gateway = self
            def on_recv_rsp(self, rsp_str):
                ret_code, content = super().on_recv_rsp(rsp_str)
                if ret_code != RET_OK:
                    return RET_ERROR, content
                try:
                    self.gateway.process_deal(content)
                except Exception as e:
                    self.gateway.write_log(f"[DealHandler] error: {e}")
                return RET_OK, content

        # 解锁
        try:
            code, data = self.trade_ctx.unlock_trade(self.password)
            if code == RET_OK:
                self.write_log(f"[{self.gateway_name}] 交易接口解锁成功")
            else:
                self.write_log(f"[{self.gateway_name}] 交易接口解锁提示: {data}")
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 交易解锁接口不可用: {e}")

        # 选账号
        self.acc_id = 0
        try:
            ret, acc_list = self.trade_ctx.get_acc_list()
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] get_acc_list 异常: {e}")
            acc_list = pd.DataFrame()
            ret = RET_ERROR

        if ret == RET_OK and not acc_list.empty:
            self.write_log(f"[{self.gateway_name}] 账户列表 (共 {len(acc_list)}个):")
            for _, row in acc_list.iterrows():
                self.write_log(
                    f"  acc_id={row['acc_id']} env={row.get('trd_env','')} "
                    f"type={row.get('acc_type','')} sim_type={row.get('sim_acc_type','N/A')} "
                    f"status={row.get('acc_status','N/A')}"
                )
            sim = acc_list[acc_list['trd_env'] == 'SIMULATE']
            if self.market == "US":
                sub = sim[sim['sim_acc_type'] == 'STOCK_AND_OPTION']
                if not sub.empty:
                    self.acc_id = int(sub.iloc[0]['acc_id'])
                    self.acc_type = str(sub.iloc[0].get('acc_type', 'MARGIN'))
                    self.write_log(f"[{self.gateway_name}] ✅ 选中: 美股模拟(股票+期权) acc_id={self.acc_id}")
                elif not sim.empty:
                    self.acc_id = int(sim.iloc[0]['acc_id'])
                    self.acc_type = str(sim.iloc[0].get('acc_type', 'MARGIN'))
                    self.write_log(f"[{self.gateway_name}] ⚠️ 降级使用首个模拟账号 acc_id={self.acc_id}")
            elif self.market == "HK":
                hk_margin = sim[(sim['sim_acc_type']=='STOCK') & (sim['acc_type']=='MARGIN')]
                hk_cash   = sim[(sim['sim_acc_type']=='STOCK') & (sim['acc_type']=='CASH')]
                if not hk_margin.empty:
                    self.acc_id = int(hk_margin.iloc[0]['acc_id']); self.acc_type = "MARGIN"
                    self.write_log(f"[{self.gateway_name}] ✅ 选中: 港股融资模拟 acc_id={self.acc_id}")
                elif not hk_cash.empty:
                    self.acc_id = int(hk_cash.iloc[0]['acc_id']); self.acc_type = "CASH"
                    self.write_log(f"[{self.gateway_name}] ⚠️ 降级现金模拟 acc_id={self.acc_id}")
                elif not sim.empty:
                    self.acc_id = int(sim.iloc[0]['acc_id'])
                    self.acc_type = str(sim.iloc[0].get('acc_type','CASH'))
        else:
            self.write_log(f"[{self.gateway_name}] ⚠️ get_acc_list 失败")

        self.trade_ctx.set_handler(OrderHandler())
        self.trade_ctx.set_handler(DealHandler())
        self.trade_ctx.start()
        self.write_log(f"[{self.gateway_name}] 交易接口连接成功 (最终 acc_id={self.acc_id})")

    # ══════════════════════════════════
    #  ★ subscribe — 只订阅基础类型
    # ══════════════════════════════════
    def subscribe(self, req: SubscribeRequest) -> None:
        """
        v3.0.1：只订阅 QUOTE + K_1M。
        美股 K_1M 用 SESSION_NONE → 仅在交易时段(09:30-16:00 美东)推送，
        避免盘前盘后稀疏报价污染 ArrayManager。
        高周期由 SubscriptionManager.subscribe_demand() 按需追加。
        """
        try:
            futu_exchange = EXCHANGE_VT2FUTU[req.exchange]
        except KeyError:
            self.write_log(f"不支持的交易所: {req.exchange}")
            return

        vt_symbol = f"{req.symbol}.{req.exchange.value}"
        if vt_symbol not in self.contracts:
            self._register_single_contract(req.symbol, req.exchange, futu_exchange)

        futu_symbol = f"{futu_exchange}.{req.symbol}"

        # ★ 仅订阅基础类型
        sub_types = [SubType.QUOTE, SubType.K_1M]

        # ★ 修复：美股 K_1M 只在交易时段推送
        session = SESSION_NONE if futu_exchange == "US" else SESSION_NONE
        self.write_log(f"[{self.gateway_name}] 订阅参数: {futu_symbol} session={session}")

        code, data = self.quote_ctx.subscribe(
            futu_symbol, sub_types,
            is_first_push=True, subscribe_push=True, session=session
        )
        if code == RET_OK:
            self.write_log(f"[{self.gateway_name}] ✅ 基础订阅成功: {futu_symbol} (QUOTE+K_1M)")
        else:
            self.write_log(f"[{self.gateway_name}] ❌ 订阅失败: {futu_symbol} | {data}")
            # 逐个退避重试
            for st in sub_types:
                c2, d2 = self.quote_ctx.subscribe(
                    futu_symbol, [st], is_first_push=True, subscribe_push=True, session=session
                )
                self.write_log(f"[{self.gateway_name}]   单独 {st}: {'OK' if c2==RET_OK else d2}")

    # ══════════════════════════════════
    #  ★ subscribe_subtypes — 供 SubscriptionManager 按需追加高周期
    # ══════════════════════════════════
    def subscribe_subtypes(self, futu_symbol: str, subtypes: list) -> bool:
        """由 SubscriptionManager 调用，追加订阅高周期类型。"""
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

        # 高周期（5M/15M/60M/日）同样只在交易时段推送
        session = SESSION_NONE if futu_symbol.startswith("US.") else SESSION_NONE
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

    @staticmethod
    def _str_to_subtype(s: str):
        mapping = {
            "QUOTE":   SubType.QUOTE,
            "K_1M":    SubType.K_1M,
            "K_5M":    SubType.K_5M,
            "K_15M":   SubType.K_15M,
            "K_30M":   SubType.K_30M,
            "K_60M":   SubType.K_60M,
            "K_DAY":   SubType.K_DAY,
            "ORDER_BOOK": SubType.ORDER_BOOK,
            "TICKER":  SubType.TICKER,
        }
        return mapping.get(s)

    # ────────────────────────────────
    #  v2.9.7 按需订阅 Tick（引用计数）
    # ────────────────────────────────
    def _vt_to_futu_symbol(self, vt_symbol: str) -> str:
        parts = vt_symbol.split(".")
        if len(parts) >= 2:
            symbol = parts[0]
            try:
                exchange = Exchange(parts[1])
            except ValueError:
                exchange = Exchange.SMART
        else:
            symbol = vt_symbol
            exchange = Exchange.SMART
        return convert_symbol_vt2futu(symbol, exchange)

    def subscribe_tick(self, vt_symbol: str) -> None:
        futu_symbol = self._vt_to_futu_symbol(vt_symbol)
        if futu_symbol in self.tick_ref_count:
            self.tick_ref_count[futu_symbol] += 1
            self.write_log(f"[TICK] 引用计数 +1: {futu_symbol} -> {self.tick_ref_count[futu_symbol]}")
            return

        self.tick_ref_count[futu_symbol] = 1
        try:
            session = SESSION_NONE if futu_symbol.startswith("US.") else SESSION_NONE
            code, data = self.quote_ctx.subscribe(
                futu_symbol, [SubType.TICKER],
                is_first_push=True, subscribe_push=True, session=session
            )
            if code == RET_OK:
                self.write_log(f"[TICK] ✅ 真实订阅 TICKER: {futu_symbol} (引用计数=1)")
            else:
                self.write_log(f"[TICK] ❌ 订阅 TICKER 失败: {futu_symbol} | {data}")
                del self.tick_ref_count[futu_symbol]
        except Exception as e:
            self.write_log(f"[TICK] ❌ 订阅 TICKER 异常: {futu_symbol} | {e}")
            if futu_symbol in self.tick_ref_count:
                del self.tick_ref_count[futu_symbol]

    def release_tick(self, vt_symbol: str) -> None:
        futu_symbol = self._vt_to_futu_symbol(vt_symbol)
        if futu_symbol not in self.tick_ref_count:
            self.write_log(f"[TICK] ⚠️ 尝试释放未订阅的 TICKER: {futu_symbol}")
            return
        self.tick_ref_count[futu_symbol] -= 1
        self.write_log(f"[TICK] 引用计数 -1: {futu_symbol} -> {self.tick_ref_count[futu_symbol]}")
        if self.tick_ref_count[futu_symbol] <= 0:
            del self.tick_ref_count[futu_symbol]
            try:
                code, data = self.quote_ctx.unsubscribe(futu_symbol, [SubType.TICKER])
                if code == RET_OK:
                    self.write_log(f"[TICK] ✅ 真实取消 TICKER 订阅: {futu_symbol}")
                else:
                    self.write_log(f"[TICK] ⚠️ 取消 TICKER 返回: {data}")
            except Exception as e:
                self.write_log(f"[TICK] ⚠️ 取消 TICKER 订阅异常: {e}")

    def _register_single_contract(self, symbol, exchange, futu_exchange) -> None:
        vt_symbol = f"{symbol}.{exchange.value}"
        futu_code = f"{futu_exchange}.{symbol}"
        try:
            ret, data = self.quote_ctx.get_stock_basicinfo(futu_exchange, SecurityType.STOCK, [futu_code])
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                c = ContractData(
                    symbol=symbol, exchange=exchange, name=row.get("name",""),
                    product=Product.EQUITY, size=1, pricetick=0.001,
                    history_data=True, net_position=True, gateway_name=self.gateway_name,
                )
                self.on_contract(c)
                self.contracts[vt_symbol] = c
                self.write_log(f"[{self.gateway_name}] [动态注册] {vt_symbol} ✅")
            else:
                self.write_log(f"[{self.gateway_name}] [动态注册] {futu_code} 失败: {data}")
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] [动态注册] 异常: {e}")

    # ══════════════════════════════════
    #  查询最大可买
    # ══════════════════════════════════
    def query_max_trd_qty(self, futu_symbol: str, price: float) -> Dict[str, int]:
        now = datetime.now().timestamp()
        if futu_symbol in self._max_trd_qty_cache:
            ts, cb, cm = self._max_trd_qty_cache[futu_symbol]
            if now - ts < self._max_trd_qty_cache_ttl:
                return {"max_cash_buy": cb, "max_cash_and_margin_buy": cm}
        try:
            code, data = self.trade_ctx.acctradinginfo_query(
                order_type=FutuOrderType.NORMAL, code=futu_symbol,
                price=price, trd_env=self.env, acc_id=self.acc_id,
            )
            if code == RET_OK and not data.empty:
                cb = int(self._safe_float(data.iloc[0].get("max_cash_buy"), 0))
                cm = int(self._safe_float(data.iloc[0].get("max_cash_and_margin_buy"), 0))
                self._max_trd_qty_cache[futu_symbol] = (now, cb, cm)
                return {"max_cash_buy": cb, "max_cash_and_margin_buy": cm}
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 查询最大可买异常: {e}")
        return {"max_cash_buy": 0, "max_cash_and_margin_buy": 0}

    # ══════════════════════════════════
    #  下单
    # ══════════════════════════════════
    def send_order(self, req: OrderRequest) -> str:
        if self.trade_ctx is None or self.acc_id == 0:
            self.write_log(f"[{self.gateway_name}] ❌ 交易未就绪，无法下单")
            return ""
        side = DIRECTION_VT2FUTU[req.direction]
        futu_symbol = f"{EXCHANGE_VT2FUTU.get(req.exchange,'US')}.{req.symbol}"
        is_buy = req.direction is Direction.LONG

        if is_buy and req.price > 0:
            info = self.query_max_trd_qty(futu_symbol, req.price)
            if self.acc_type.upper() == "MARGIN":
                max_allowed = info["max_cash_and_margin_buy"]; label = "融资最大可买"
            else:
                max_allowed = info["max_cash_buy"]; label = "现金最大可买"
            if max_allowed > 0 and req.volume > max_allowed:
                self.write_log(f"[{self.gateway_name}] ⚠️ 资金预检: {req.volume}→{max_allowed} ({label})")
                req.volume = max_allowed
            elif max_allowed == 0 and self.acc_info.get("power", 0) > 0:
                est = int(self.acc_info["power"] * 0.95 / req.price)
                if req.volume > est > 0:
                    self.write_log(f"[{self.gateway_name}] ⚠️ 估算缩减: {req.volume}→{est}")
                    req.volume = est

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
            orderid = str(row.get("order_id", ""))
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
        if self.trade_ctx is None:
            return
        code, data = self.trade_ctx.modify_order(
            ModifyOrderOp.CANCEL, req.orderid, 0, 0, trd_env=self.env, acc_id=self.acc_id
        )
        if code:
            self.write_log(f"[{self.gateway_name}] 撤单失败: {data}")
        else:
            self.write_log(f"[{self.gateway_name}] 撤单已发送: {req.orderid}")

    # ══════════════════════════════════
    #  定时查询
    # ══════════════════════════════════
    def query_data(self) -> None:
        sleep(2.0)
        try:
            self.query_contract()
            self.query_trade()
            self.query_order()
            self.query_position()
            self.query_account()
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] query_data 异常: {e}")
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_timer_event(self, event) -> None:
        self.count += 1
        if self.count < self.interval:
            return
        self.count = 0
        if not self.query_funcs:
            return
        func = self.query_funcs.pop(0)
        try:
            func()
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] timer func 异常: {e}")
        self.query_funcs.append(func)

    # ══════════════════════════════════
    #  账户 / 持仓 / 订单 / 成交 / 合约
    # ══════════════════════════════════
    def query_account(self) -> None:
        if self.acc_id == 0 or self.trade_ctx is None:
            return
        try:
            code, data = self.trade_ctx.accinfo_query(trd_env=self.env, acc_id=self.acc_id)
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 查询账户异常: {e}")
            return
        if code or data.empty:
            self.write_log(f"[{self.gateway_name}] 查询账户失败: {data}")
            return

        for _, row in data.iterrows():
            total = self._safe_float(row.get("total_assets"), 0)
            cash  = self._safe_float(row.get("cash"), total)
            mval  = self._safe_float(row.get("market_val"), 0)
            frozen = self._safe_float(row.get("frozen_cash"), 0)
            power = self._safe_float(row.get("power"), 0)

            if self.market == "US":
                sc  = self._safe_float(row.get("us_cash"), cash)
                sp  = self._safe_float(row.get("usd_net_cash_power"), power)
                cur = "USD"
            else:
                sc = self._safe_float(row.get("hk_cash"), cash)
                real_hkd = self._safe_float(row.get("hkd_net_cash_power"), None)
                if real_hkd and real_hkd > 0:
                    sp = real_hkd
                elif power > 0:
                    sp = power
                else:
                    sp = sc * 2.0 if self.acc_type == "MARGIN" else sc
                cur = "HKD"

            if not self._debug_dumped:
                self._debug_dumped = True
                self.write_log(f"[{self.gateway_name}] 调试 accinfo 字段: {list(data.columns)}")

            self.acc_info = {
                "gateway": self.gateway_name, "acc_id": self.acc_id,
                "total_assets": total, "cash": sc, "raw_cash": cash,
                "market_val": mval, "frozen_cash": frozen, "power": sp,
                "currency": cur, "market": self.market, "acc_type": self.acc_type,
            }
            self.on_account(AccountData(
                accountid=f"{self.gateway_name}_{self.acc_id}",
                balance=total, frozen=frozen, gateway_name=self.gateway_name
            ))
            self.write_log(
                f"[{self.gateway_name}] 账户: 总资产=${total:,.2f} "
                f"现金=${sc:,.2f} 证券=${mval:,.2f} 冻结=${frozen:,.2f} 购买力=${sp:,.2f} ({cur})"
            )

    def query_position(self) -> None:
        if self.acc_id == 0 or self.trade_ctx is None:
            return
        try:
            code, data = self.trade_ctx.position_list_query(trd_env=self.env, acc_id=self.acc_id)
        except Exception as e:
            self.write_log(f"[{self.gateway_name}] 查询持仓异常: {e}")
            return
        if code or data.empty:
            return
        for _, row in data.iterrows():
            sym, ex = convert_symbol_futu2vt(row["code"])
            qty  = self._safe_float(row.get("qty"), 0)
            can  = self._safe_float(row.get("can_sell_qty", qty), qty)
            self.on_position(PositionData(
                symbol=sym, exchange=ex, direction=Direction.NET,
                volume=int(qty), frozen=qty - can,
                price=self._safe_float(row.get("cost_price"), 0),
                pnl=self._safe_float(row.get("pl_val"), 0),
                gateway_name=self.gateway_name
            ))

    def query_order(self) -> None:
        if self.acc_id == 0 or self.trade_ctx is None:
            return
        code, data = self.trade_ctx.order_list_query("", trd_env=self.env, acc_id=self.acc_id)
        if code:
            return
        self.process_order(data)

    def query_trade(self) -> None:
        if self.acc_id == 0 or self.trade_ctx is None:
            return
        code, data = self.trade_ctx.deal_list_query("", trd_env=self.env, acc_id=self.acc_id)
        if code:
            self.write_log(f"[{self.gateway_name}] 查询成交失败: {data}")
            return
        self.process_deal(data)

    def query_contract(self) -> None:
        market = "HK" if self.market in ["HK", "HK_FUTURE"] else self.market
        count = 0
        for sec_type, vt_prod in SEC_TYPE_FUTU2VT.items():
            try:
                code, data = self.quote_ctx.get_stock_basicinfo(market, sec_type)
            except Exception as e:
                self.write_log(f"[{self.gateway_name}] get_stock_basicinfo 异常: {e}")
                continue
            if code or data is None or data.empty:
                continue
            for _, row in data.iterrows():
                sym, ex = convert_symbol_futu2vt(row["code"])
                c = ContractData(
                    symbol=sym, exchange=ex, name=row.get("name",""),
                    product=vt_prod, size=1, pricetick=0.001,
                    history_data=True, net_position=True, gateway_name=self.gateway_name,
                )
                self.on_contract(c)
                self.contracts[c.vt_symbol] = c
                count += 1
        self.write_log(f"[{self.gateway_name}] 合约查询完成: {count}个")
        self.event_engine.put(Event("eContractReady", self.gateway_name))

    # ══════════════════════════════════
    #  关闭
    # ══════════════════════════════════
    def close(self) -> None:
        if self.tick_ref_count:
            self.write_log(f"[{self.gateway_name}] 关闭时清理 {len(self.tick_ref_count)} 个 TICKER 订阅")
            for futu_symbol in list(self.tick_ref_count.keys()):
                try:
                    self.quote_ctx.unsubscribe(futu_symbol, [SubType.TICKER])
                except Exception:
                    pass
            self.tick_ref_count.clear()

        for ctx, name in [(self.quote_ctx, "quote"), (self.trade_ctx, "trade")]:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception as e:
                    self.write_log(f"[{self.gateway_name}] 关闭{name}异常: {e}")

        self.quote_ctx = None
        self.trade_ctx = None

        with self._db_lock:
            if self._tick_db_conn:
                try:
                    self._tick_db_conn.commit()
                    self._tick_db_conn.close()
                except Exception:
                    pass
                self._tick_db_conn = None
        print(f"[DB STATS] [{self.gateway_name}] 共接收 {self._tick_count} ticks", flush=True)

    # ────────────────────────────────
    #  get_tick（带去重）
    # ────────────────────────────────
    def get_tick(self, code_str) -> TickData:
        tick = self.ticks.get(code_str)
        sym, ex = convert_symbol_futu2vt(code_str)
        if not tick:
            tick = TickData(
                symbol=sym, exchange=ex,
                datetime=datetime.now(CHINA_TZ),
                gateway_name=self.gateway_name,
            )
            self.ticks[code_str] = tick
        else:
            if not tick.symbol:
                tick.symbol = sym
                tick.exchange = ex
                tick.gateway_name = self.gateway_name
        contract = self.contracts.get(tick.vt_symbol)
        if contract and not tick.name:
            tick.name = contract.name
        return tick

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        bars: List[BarData] = []
        if req.interval != Interval.MINUTE:
            self.write_log(f"[{self.gateway_name}] FUTU仅支持分钟线")
            return bars
        if self.quote_ctx is None:
            return bars
        futu_symbol = f"{EXCHANGE_VT2FUTU.get(req.exchange,'US')}.{req.symbol}"
        start = req.start.replace(tzinfo=None).strftime("%Y-%m-%d")
        end   = req.end.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        ret, df, key = self.quote_ctx.request_history_kline(
            code=futu_symbol, start=start, end=end, ktype=KLType.K_1M
        )
        if ret != RET_OK:
            self.write_log(f"[{self.gateway_name}] 获取K线失败: {df}")
            return bars
        while key is not None:
            ret, more, key = self.quote_ctx.request_history_kline(
                code=futu_symbol, start=start, end=end, ktype=KLType.K_1M, page_req_key=key
            )
            if ret == RET_OK and more is not None:
                df = pd.concat([df, more], ignore_index=True)
        df["time_key"] = pd.to_datetime(df["time_key"]) - pd.Timedelta(1, "m")
        for _, row in df.iterrows():
            bars.append(BarData(
                gateway_name=self.gateway_name, symbol=req.symbol, exchange=req.exchange,
                datetime=generate_datetime(row["time_key"].strftime("%Y-%m-%d %H:%M:%S")),
                interval=Interval.MINUTE, volume=self._safe_float(row.get("volume")),
                turnover=self._safe_float(row.get("turnover")),
                open_interest=0,
                open_price=self._safe_float(row.get("open")),
                high_price=self._safe_float(row.get("high")),
                low_price=self._safe_float(row.get("low")),
                close_price=self._safe_float(row.get("close")),
            ))
        return bars

    # ────────────────────────────────
    #  process_quote / orderbook / ticker
    # ────────────────────────────────
    def process_quote(self, data) -> None:
        for _, row in data.iterrows():
            code_str = row.get("code", "")
            if not code_str:
                continue
            tick = self.get_tick(code_str)

            date = str(row.get("data_date", "")).replace("-", "")
            t = str(row.get("data_time", ""))
            ts = f"{date} {t}"
            try:
                tick.datetime = datetime.strptime(
                    ts, "%Y%m%d %H:%M:%S.%f" if "." in ts else "%Y%m%d %H:%M:%S"
                ).replace(tzinfo=CHINA_TZ)
            except ValueError:
                tick.datetime = datetime.now(CHINA_TZ)

            tick.open_price  = self._safe_float(row.get("open_price"), 0)
            tick.high_price  = self._safe_float(row.get("high_price"), 0)
            low_r = self._safe_float(row.get("low_price"), None)
            prev  = self._safe_float(row.get("prev_close_price"), None)
            op    = self._safe_float(row.get("open_price"), 0)
            tick.low_price = low_r if low_r else (prev if prev else op)
            tick.pre_close  = prev if prev else op
            tick.last_price = self._safe_float(row.get("last_price"), 0)
            tick.volume     = self._safe_float(row.get("volume"), 0)
            tick.turnover   = self._safe_float(row.get("turnover"), 0)

            if self._should_save_tick(tick.vt_symbol):
                self._save_tick_to_db(tick, source='quote')
            self.on_tick(copy(tick))

    def process_orderbook(self, data) -> None:
        code_str = data.get("code", "")
        if not code_str:
            return
        tick = self.get_tick(code_str)
        d = tick.__dict__
        bids = data.get("Bid", [])
        asks = data.get("Ask", [])
        for i in range(5):
            n = i + 1
            if i < len(bids) and i < len(asks):
                b, a = bids[i], asks[i]
                d[f"bid_price_{n}"] = self._safe_float(b[0], 0)
                d[f"bid_volume_{n}"] = self._safe_int(b[1], 0)
                d[f"ask_price_{n}"] = self._safe_float(a[0], 0)
                d[f"ask_volume_{n}"] = self._safe_int(a[1], 0)
            else:
                d[f"bid_price_{n}"] = 0.0
                d[f"bid_volume_{n}"] = 0
                d[f"ask_price_{n}"] = 0.0
                d[f"ask_volume_{n}"] = 0
        if tick.datetime:
            if self._should_save_tick(tick.vt_symbol):
                self._save_tick_to_db(tick, source='orderbook')
            self.on_tick(copy(tick))

    def process_ticker(self, data) -> None:
        for _, row in data.iterrows():
            code_str = row.get("code", "")
            if not code_str:
                continue
            tick = self.get_tick(code_str)

            t_str = str(row.get("time", ""))
            if t_str:
                try:
                    tick.datetime = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=CHINA_TZ)
                except ValueError:
                    try:
                        tick.datetime = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TZ)
                    except ValueError:
                        pass

            price = self._safe_float(row.get("price"), 0)
            vol   = self._safe_float(row.get("volume"), 0)
            if price > 0:
                tick.last_price = price
            if not tick.volume and vol > 0:
                tick.volume = vol

            if self._should_save_tick(tick.vt_symbol):
                self._save_tick_to_db(tick, source='ticker')
            self.on_tick(copy(tick))

    # ────────────────────────────────
    #  process_order / process_deal
    # ────────────────────────────────
    def process_order(self, data) -> None:
        for _, row in data.iterrows():
            if row.get("order_status") == OrderStatus.DELETED:
                continue
            try:
                direction, offset = DIRECTION_FUTU2VT[row["trd_side"]]
            except KeyError:
                continue
            sym, ex = convert_symbol_futu2vt(row["code"])
            oid = str(row.get("order_id", ""))
            order = OrderData(
                symbol=sym, exchange=ex, orderid=oid,
                direction=direction, offset=offset,
                price=self._safe_float(row.get("price"), 0),
                volume=int(self._safe_float(row.get("qty"), 0)),
                traded=int(self._safe_float(row.get("dealt_qty"), 0)),
                status=STATUS_FUTU2VT.get(row.get("order_status"), Status.SUBMITTING),
                datetime=generate_datetime(str(row.get("create_time", ""))),
                gateway_name=self.gateway_name,
            )
            order.vt_orderid = f"{self.gateway_name}.{oid}"
            self.orders[order.vt_orderid] = order
            self.orders[oid] = order
            self.on_order(order)

    def process_deal(self, data) -> None:
        for _, row in data.iterrows():
            tid = str(row.get("deal_id", ""))
            if not tid or tid in self.trades:
                continue
            self.trades.add(tid)
            try:
                direction, offset = DIRECTION_FUTU2VT[row["trd_side"]]
            except KeyError:
                continue
            sym, ex = convert_symbol_futu2vt(row["code"])
            self.on_trade(TradeData(
                symbol=sym, exchange=ex, direction=direction, offset=offset,
                tradeid=tid, orderid=str(row.get("order_id", "")),
                price=self._safe_float(row.get("price"), 0),
                volume=int(self._safe_float(row.get("qty"), 0)),
                datetime=generate_datetime(str(row.get("create_time", ""))),
                gateway_name=self.gateway_name,
            ))


# ══════════════════════════════════
#  Datafeed 兼容
# ══════════════════════════════════
class FutuDatafeed:
    def __init__(self):
        self.name = "FutuDatafeed"
