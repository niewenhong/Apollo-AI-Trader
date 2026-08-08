# -*- coding: utf-8 -*-
"""
core/order_manager.py - Apollo Trader v3.8.0 融合版
基于 v3.6.0 基线，完整融合 v3.8.0 全部新特性：

v3.6.0 基线保留：
- OrderData / TradeRecord 数据结构
- TradeSource 抽象基类
- LiveTradeSource（生产环境交易源，对接 futu-api）
- SimTradeSource（模拟成交源，含概率成交引擎）
- OrderManager（vnpy 风格订单管理，含账户轮询、数据库持久化）

v3.8.0 新增融合：
- submit_signal() 智能路由入口（多用户隔离）
- on_order_fill() / on_order_reject() / on_order_cancel() 回调
- register_fill_callback() 按 symbol 注册回调
- get_fill_rate() 填充率统计
- get_user_stats() / get_strategy_stats() 多维统计
- update_equity() 用户级权益跟踪
- get_net_qty() 净持仓查询
- reset_daily() 每日统计重置
- get_status() 状态摘要
- LiveTradeSource / SimTradeSource 支持 on_trade 回调链
- OrderManager 构造兼容 trade_source 注入
"""
import time
import threading
import logging
import traceback
from datetime import datetime
from collections import defaultdict, deque
from typing import Optional, Dict, List, Callable, Any, Union
from dataclasses import dataclass, field
from queue import Queue, Empty

try:
    from futu import (
        TrdSide, OrderType as FutuOrderType, ModifyOrderOp,
        OrderStatus, RET_OK, RET_ERROR,
    )
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False

# vnpy 为可选依赖（沙盒/纯模拟环境可能没有）
try:
    from vnpy.trader.constant import Direction, Offset, Status, Exchange
    from vnpy.trader.object import OrderData as VnpyOrderData
    from vnpy.trader.object import TradeData as VnpyTradeData
    from vnpy.trader.object import AccountData as VnpyAccountData
    VNPY_AVAILABLE = True
except ImportError:
    Direction = Offset = Status = Exchange = None
    VnpyOrderData = VnpyTradeData = VnpyAccountData = None
    VNPY_AVAILABLE = False
    logger = logging.getLogger("OrderManager")
    logger.warning("[OrderManager] vnpy 未安装，部分功能降级运行")

logger = logging.getLogger("OrderManager")


# ==================== 数据结构 ====================

@dataclass
class OrderData:
    """统一订单数据结构（v3.6.0 基线保留）"""
    order_id: str
    symbol: str
    direction: str          # BUY / SELL
    order_type: str         # MARKET / LIMIT
    price: float
    total_qty: int
    traded_qty: int = 0
    status: str = "SUBMITTED"
    create_time: float = 0.0
    update_time: float = 0.0
    error_msg: str = ""
    ext: dict = field(default_factory=dict)


@dataclass
class TradeRecord:
    """成交记录（v3.6.0 基线保留）"""
    trade_id: str
    order_id: str
    symbol: str
    direction: str
    price: float
    qty: int
    trade_time: float
    commission: float = 0.0


# ==================== 抽象基类 ====================

class TradeSource:
    """
    交易来源抽象基类（v3.6.0 基线保留）
    定义统一的交易源接口，子类实现具体逻辑
    """
    def __init__(self, gw=None, poll_interval: float = 2.0):
        self.gw = gw
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._orders: Dict[str, OrderData] = {}
        self._trades: List[TradeRecord] = []
        self._lock = threading.Lock()
        self._on_trade_callbacks: List[Callable] = []
        self._on_order_callbacks: List[Callable] = []
        self._status_filter_list = [
            "SUBMITTED", "PARTIAL_FILLED", "FILLED_ALL", "FILLED_PART",
            "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DELETED", "DISABLED"
        ]

    # --- 生命周期 ---
    def start(self):
        """启动交易源"""
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name=f"{self.__class__.__name__}-Poll")
        self._thread.start()
        logger.info(f"[{self.__class__.__name__}] ✅ 已启动 (间隔 {self.poll_interval}s)")

    def stop(self):
        """停止交易源"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"[{self.__class__.__name__}] ⏹ 已停止")

    # --- 抽象方法 ---
    def place_order(self, symbol: str, direction: str, order_type: str,
                    price: float, qty: int, **kwargs) -> Optional[str]:
        """下单，返回 order_id"""
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        raise NotImplementedError

    def get_order(self, order_id: str) -> Optional[OrderData]:
        """查询单笔订单"""
        raise NotImplementedError

    def get_trades(self) -> List[TradeRecord]:
        """获取全部成交"""
        raise NotImplementedError

    # --- 回调注册（v3.8.0 增强） ---
    def register_on_trade(self, callback: Callable):
        """注册成交回调"""
        self._on_trade_callbacks.append(callback)

    def register_on_order(self, callback: Callable):
        """注册订单状态回调（v3.8.0 新增）"""
        self._on_order_callbacks.append(callback)

    # --- 内部方法 ---
    def _poll_loop(self):
        """轮询循环（子类可重写）"""
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] 轮询异常: {e}")
            time.sleep(self.poll_interval)

    def _poll_once(self):
        """单次轮询（子类实现）"""
        pass

    def _emit_trade(self, trade: TradeRecord):
        """触发成交回调"""
        with self._lock:
            self._trades.append(trade)
        for cb in self._on_trade_callbacks:
            try:
                cb(trade)
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] trade callback error: {e}")

    def _emit_order(self, order: OrderData):
        """触发订单回调（v3.8.0 新增）"""
        for cb in self._on_order_callbacks:
            try:
                cb(order)
            except Exception as e:
                logger.error(f"[{self.__class__.__name__}] order callback error: {e}")


# ==================== 生产环境交易源 ====================

class LiveTradeSource(TradeSource):
    """
    生产环境交易源（v3.6.0 基线保留）
    对接真实 futu-api 交易接口
    """
    def __init__(self, gw=None, poll_interval: float = 2.0):
        super().__init__(gw=gw, poll_interval=poll_interval)
        self.trade_ctx = None
        self.quote_ctx = None
        if gw is not None:
            self.trade_ctx = getattr(gw, 'trade_ctx', None)
            self.quote_ctx = getattr(gw, 'quote_ctx', None)
        self.acc_id = 0
        self.env = None
        if gw is not None:
            self.env = getattr(gw, 'env', None)
        self._order_map: Dict[str, str] = {}  # futu_order_id -> local_order_id
        logger.info("[LiveTradeSource] initialized")

    def set_contexts(self, trade_ctx, quote_ctx=None):
        """设置交易/行情上下文"""
        self.trade_ctx = trade_ctx
        self.quote_ctx = quote_ctx

    def set_account(self, acc_id: int, env=None):
        """设置账户"""
        self.acc_id = acc_id
        if env is not None:
            self.env = env

    def place_order(self, symbol: str, direction: str, order_type: str,
                    price: float, qty: int, **kwargs) -> Optional[str]:
        """向富途提交真实订单"""
        if self.trade_ctx is None:
            logger.error("[LiveTradeSource] ❌ trade_ctx 未设置")
            return None

        try:
            from futu import TrdSide as FS, OrderType as FO
            trd_side = FS.BUY if direction.upper() == "BUY" else FS.SELL
            ord_type = FO.NORMAL if order_type.upper() == "LIMIT" else FO.MARKET

            adj = kwargs.get("adjust_limit", 0.05)
            code = kwargs.get("code", symbol)
            if not code or "." not in str(code):
                code = self._format_code(symbol)

            ret, data = self.trade_ctx.place_order(
                price=price, qty=qty, code=code,
                trd_side=trd_side, order_type=ord_type,
                trd_env=self.env, acc_id=self.acc_id,
                adjust_limit=adj,
            )
            if ret == RET_OK and data is not None and not data.empty:
                futu_oid = str(data.iloc[0].get("order_id", ""))
                local_oid = f"LIVE_{int(time.time()*1000)}_{futu_oid[-6:]}"
                self._order_map[futu_oid] = local_oid

                order = OrderData(
                    order_id=local_oid,
                    symbol=symbol,
                    direction=direction.upper(),
                    order_type=order_type.upper(),
                    price=price,
                    total_qty=qty,
                    create_time=time.time(),
                    update_time=time.time(),
                    ext={"futu_order_id": futu_oid, "code": code}
                )
                with self._lock:
                    self._orders[local_oid] = order
                self._emit_order(order)
                logger.info(f"[LiveTradeSource] 📤 {local_oid} {direction} {symbol} {qty}@{price:.2f}")
                return local_oid
            else:
                err = str(data) if data is not None else "下单失败"
                logger.error(f"[LiveTradeSource] ❌ 下单失败: {symbol} | {err}")
                return None
        except Exception as e:
            logger.error(f"[LiveTradeSource] 下单异常: {e}\n{traceback.format_exc()}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        if self.trade_ctx is None:
            return False
        futu_oid = self._order_map.get(order_id, order_id)
        try:
            ret, data = self.trade_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=futu_oid, qty=0, price=0,
                trd_env=self.env, acc_id=self.acc_id,
            )
            if ret == RET_OK:
                with self._lock:
                    order = self._orders.get(order_id)
                    if order:
                        order.status = "CANCELLED_ALL"
                        order.update_time = time.time()
                logger.info(f"[LiveTradeSource] ⏹ 撤单成功: {order_id}")
                return True
            logger.error(f"[LiveTradeSource] 撤单失败: {order_id} | {data}")
        except Exception as e:
            logger.error(f"[LiveTradeSource] 撤单异常: {e}")
        return False

    def get_order(self, order_id: str) -> Optional[OrderData]:
        with self._lock:
            return self._orders.get(order_id)

    def get_trades(self) -> List[TradeRecord]:
        with self._lock:
            return list(self._trades)

    def _poll_once(self):
        """轮询订单和成交"""
        if self.trade_ctx is None:
            return
        try:
            # 查询订单
            ret, data = self.trade_ctx.order_list_query(
                status_filter_list=[],
                trd_env=self.env, acc_id=self.acc_id,
                refresh_cache=True,
            )
            if ret == RET_OK and data is not None and not data.empty:
                self._process_order_data(data)

            # 查询成交（仅实盘）
            if self.env and str(self.env).upper() != "SIMULATE":
                ret2, deals = self.trade_ctx.deal_list_query(
                    "", trd_env=self.env, acc_id=self.acc_id
                )
                if ret2 == RET_OK and deals is not None and not deals.empty:
                    self._process_deal_data(deals)
        except Exception as e:
            logger.error(f"[LiveTradeSource] 轮询异常: {e}")

    def _process_order_data(self, data):
        """处理订单数据"""
        for _, row in data.iterrows():
            futu_oid = str(row.get("order_id", ""))
            local_oid = self._order_map.get(futu_oid, futu_oid)
            with self._lock:
                order = self._orders.get(local_oid)
                if order is None:
                    order = OrderData(
                        order_id=local_oid,
                        symbol=str(row.get("code", "")).split(".")[-1],
                        direction=str(row.get("trd_side", "")),
                        order_type="LIMIT",
                        price=float(row.get("price", 0)),
                        total_qty=int(row.get("qty", 0)),
                        create_time=time.time(),
                    )
                    self._orders[local_oid] = order

                dealt = int(row.get("dealt_qty", 0))
                order.traded_qty = dealt
                status_raw = row.get("order_status", "")
                if "FILL" in str(status_raw).upper():
                    order.status = "FILLED_ALL" if dealt >= order.total_qty else "PARTIAL_FILLED"
                elif "CANCEL" in str(status_raw).upper():
                    order.status = "CANCELLED_ALL"
                elif "FAIL" in str(status_raw).upper():
                    order.status = "FAILED"
                else:
                    order.status = "SUBMITTED"
                order.update_time = time.time()
            self._emit_order(order)

    def _process_deal_data(self, deals):
        """处理成交数据"""
        for _, row in deals.iterrows():
            tid = str(row.get("deal_id", ""))
            # 去重
            with self._lock:
                existing_ids = {t.trade_id for t in self._trades}
            if tid in existing_ids:
                continue

            trade = TradeRecord(
                trade_id=tid,
                order_id=self._order_map.get(str(row.get("order_id", "")), str(row.get("order_id", ""))),
                symbol=str(row.get("code", "")).split(".")[-1],
                direction=str(row.get("trd_side", "")),
                price=float(row.get("price", 0)),
                qty=int(row.get("qty", 0)),
                trade_time=time.time(),
            )
            self._emit_trade(trade)

    def _format_code(self, symbol: str) -> str:
        """格式化代码"""
        if "." in symbol:
            return symbol
        if symbol.isdigit() and len(symbol) == 5:
            return f"HK.{symbol}"
        return f"US.{symbol}"


# ==================== 模拟交易源 ====================

class SimTradeSource(TradeSource):
    """
    模拟成交源（v3.6.0 基线保留 + v3.8.0 增强）
    用于回测/模拟盘，按概率模拟成交
    """
    def __init__(self, gw=None, poll_interval: float = 2.0):
        super().__init__(gw=gw, poll_interval=poll_interval)
        self._fill_probability = 0.35       # 单次轮询成交概率
        self._partial_fill_prob = 0.5         # 部分成交概率
        self._max_partial_pct = 0.3          # 最大部分成交比例
        self._slippage = 0.001               # 滑点
        self._last_prices: Dict[str, float] = {}  # 最新价缓存
        self._subscribed_symbols: set = set()
        logger.info("[SimTradeSource] initialized")

    def set_fill_probability(self, prob: float):
        self._fill_probability = max(0.0, min(1.0, prob))

    def set_last_price(self, symbol: str, price: float):
        """外部设置最新价（由行情推送驱动）"""
        self._last_prices[symbol] = price

    def subscribe_price(self, symbol: str):
        """订阅价格更新"""
        self._subscribed_symbols.add(symbol)

    def place_order(self, symbol: str, direction: str, order_type: str,
                    price: float, qty: int, **kwargs) -> Optional[str]:
        """模拟下单"""
        order_id = f"SIM_{int(time.time()*1000)}_{symbol}_{direction}"
        order = OrderData(
            order_id=order_id,
            symbol=symbol,
            direction=direction.upper(),
            order_type=order_type.upper(),
            price=price,
            total_qty=qty,
            create_time=time.time(),
            update_time=time.time(),
            ext=kwargs,
        )
        with self._lock:
            self._orders[order_id] = order
        self._emit_order(order)
        logger.info(f"[SimTradeSource] 📤 {order_id} {direction} {symbol} {qty}@{price:.2f}")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if order and order.status in ("SUBMITTED", "PARTIAL_FILLED"):
                order.status = "CANCELLED_ALL"
                order.update_time = time.time()
                self._emit_order(order)
                logger.info(f"[SimTradeSource] ⏹ 撤单: {order_id}")
                return True
        return False

    def get_order(self, order_id: str) -> Optional[OrderData]:
        with self._lock:
            return self._orders.get(order_id)

    def get_trades(self) -> List[TradeRecord]:
        with self._lock:
            return list(self._trades)

    def _poll_once(self):
        """模拟成交引擎"""
        import random
        with self._lock:
            orders = list(self._orders.items())

        for oid, order in orders:
            if order.status not in ("SUBMITTED", "PARTIAL_FILLED"):
                continue
            remaining = order.total_qty - order.traded_qty
            if remaining <= 0:
                continue

            # 成交概率
            if random.random() < self._fill_probability:
                # 决定成交数量
                if random.random() < self._partial_fill_prob:
                    fill_pct = random.uniform(0.1, self._max_partial_pct)
                    fill_qty = max(1, int(remaining * fill_pct))
                else:
                    fill_qty = remaining

                fill_qty = min(fill_qty, remaining)

                # 成交价（含滑点）
                slip = self._slippage if order.direction == "BUY" else -self._slippage
                # 使用缓存的最新价或订单价
                base_price = self._last_prices.get(order.symbol, order.price)
                fill_price = round(base_price * (1 + slip), 4)

                trade = TradeRecord(
                    trade_id=f"TRD_{int(time.time()*1000000)}_{oid[-8:]}",
                    order_id=oid,
                    symbol=order.symbol,
                    direction=order.direction,
                    price=fill_price,
                    qty=fill_qty,
                    trade_time=time.time(),
                )

                with self._lock:
                    order.traded_qty += fill_qty
                    if order.traded_qty >= order.total_qty:
                        order.status = "FILLED_ALL"
                    else:
                        order.status = "PARTIAL_FILLED"
                    order.update_time = time.time()

                self._emit_trade(trade)
                logger.info(f"[SimTradeSource] ✅ 成交: {trade.trade_id} {order.symbol} {fill_qty}@{fill_price:.4f}")


# ==================== 订单管理器（v3.8.0 增强版） ====================

class OrderManager:
    """
    订单管理器 v3.8.0（基于 v3.6.0 基线融合）

    v3.6.0 保留：
    - gateways 字典（多网关管理）
    - account_equity 账户权益
    - trade_source 交易源注入
    - update_account_equity() 权益更新
    - start_account_polling() 账户轮询
    - place_order() / cancel_order() 下单撤单
    - get_order() / get_all_orders() / get_trades()
    - _on_trade_callback() 成交回调处理

    v3.8.0 新增：
    - submit_signal() 智能路由入口（多用户隔离）
    - on_order_fill() / on_order_reject() / on_order_cancel()
    - register_fill_callback() 按 symbol 注册回调
    - get_fill_rate() 填充率统计
    - get_user_stats() / get_strategy_stats() 多维统计
    - update_equity() 用户级权益跟踪
    - get_net_qty() 净持仓查询
    - reset_daily() 每日统计重置
    - get_status() 状态摘要
    - _route_signal() 智能路由
    - _detect_market() 市场自动识别
    - _calc_pnl() 盈亏计算
    """
    def __init__(self, gateways: dict = None, account_equity: float = 100000.0,
                 trade_source: Optional[TradeSource] = None, **kwargs):
        """
        :param gateways: {market: gateway_instance} e.g. {"US": gw_us, "HK": gw_hk}
        :param account_equity: 初始账户权益
        :param trade_source: 交易源（LiveTradeSource / SimTradeSource）
        :param kwargs: 额外参数（兼容 v3.8.0 调用方式）
        """
        # v3.6.0 基础属性
        self.gateways = gateways or {}
        self.account_equity = account_equity
        self.trade_source = trade_source

        # 订单/成交存储
        self._orders: Dict[str, dict] = {}
        self._trades: List[TradeRecord] = []
        self._lock = threading.Lock()
        self._running = False

        # 账户轮询
        self._polling = False
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_interval = kwargs.get("poll_interval", 60.0)

        # 持仓管理
        self.position_manager = None

        # v3.8.0 新增：多用户统计
        self._user_stats: Dict[str, dict] = defaultdict(lambda: {
            'sent': 0, 'filled': 0, 'rejected': 0, 'cancelled': 0
        })
        self._strategy_stats: Dict[str, dict] = defaultdict(lambda: {
            'sent': 0, 'filled': 0, 'pnl': 0.0
        })
        self._equity: Dict[str, dict] = {}
        self._fill_callbacks: Dict[str, Callable] = {}

        # v3.8.0 新增：信号队列
        self._signal_queue: Queue = Queue()
        self._signal_worker: Optional[threading.Thread] = None
        self._signal_running = False

        # 如果传入了 trade_source，注册回调
        if self.trade_source:
            self.trade_source.register_on_trade(self._on_trade_callback)
            self.trade_source.register_on_order(self._on_order_callback)

        logger.info(f"[OrderManager] ✅ 初始化完成 (v3.8.0) "
                     f"gateways={list(self.gateways.keys())} "
                     f"equity={account_equity:,.2f}")

    # ==================== v3.6.0 原有方法 ====================

    def update_account_equity(self, equity_info) -> None:
        """更新账户权益（v3.6.0 原有方法）"""
        import pandas as pd
        if isinstance(equity_info, pd.DataFrame):
            if equity_info.empty:
                return
            row = equity_info.iloc[0]
            new_val = None
            for col in ('total_assets', 'net_assets', 'cash', 'power'):
                if col in equity_info.columns:
                    try:
                        new_val = float(row[col])
                        break
                    except (ValueError, TypeError):
                        continue
            if new_val is None:
                logger.warning(f"[OrderManager] 未识别权益字段: {list(equity_info.columns)}")
                return
        elif isinstance(equity_info, (int, float)):
            new_val = float(equity_info)
        elif isinstance(equity_info, dict):
            new_val = float(equity_info.get("total_assets", equity_info.get("cash", 0)))
        else:
            logger.warning(f"[OrderManager] 不支持的 equity_info 类型: {type(equity_info)}")
            return

        old = self.account_equity
        self.account_equity = new_val
        logger.info(f"[OrderManager] 账户权益更新: {old:,.2f} → {new_val:,.2f} (变化: {new_val-old:+,.2f})")

    def start_account_polling(self, interval: float = 30.0) -> None:
        """启动账户权益轮询（v3.6.0 原有方法）"""
        self._poll_interval = interval
        self._polling = True
        self._poll_thread = threading.Thread(
            target=self._account_poll_loop, daemon=True, name="OrderMgr-Poll"
        )
        self._poll_thread.start()
        logger.info(f"[OrderManager] ✅ 账户权益轮询已启动 (间隔 {interval}s)")

    def stop_account_polling(self) -> None:
        """停止账户轮询"""
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("[OrderManager] 账户轮询已停止")

    def _account_poll_loop(self) -> None:
        """账户轮询循环"""
        while self._polling:
            try:
                for name, gw in self.gateways.items():
                    if hasattr(gw, 'trade_ctx') and gw.trade_ctx is not None:
                        try:
                            from futu import TrdEnv
                            env = getattr(gw, 'env', None) or TrdEnv.SIMULATE
                            ret, data = gw.trade_ctx.accinfo_query(trd_env=env)
                            if ret == RET_OK and data is not None and not data.empty:
                                self.update_account_equity(data)
                            else:
                                logger.warning(f"[OrderManager] accinfo_query 失败 ({name}): {data}")
                        except Exception as e:
                            logger.warning(f"[OrderManager] {name} 查询异常: {e}")
                    elif hasattr(gw, 'query_account') and callable(gw.query_account):
                        try:
                            result = gw.query_account()
                            if result:
                                self.update_account_equity(result)
                        except Exception as e:
                            logger.warning(f"[OrderManager] {name} query_account 异常: {e}")
            except Exception as e:
                logger.error(f"[OrderManager] 轮询异常: {e}")
            time.sleep(self._poll_interval)

    def start(self) -> None:
        """启动 OrderManager（v3.6.0 原有方法）"""
        self._running = True
        if self.trade_source:
            self.trade_source.start()
        # 启动信号处理线程
        self._signal_running = True
        self._signal_worker = threading.Thread(
            target=self._signal_loop, daemon=True, name="OrderMgr-Signal"
        )
        self._signal_worker.start()
        logger.info("[OrderManager] ✅ 已启动（含 trade_source + signal_worker）")

    def stop(self) -> None:
        """停止 OrderManager"""
        self._running = False
        self._signal_running = False
        if self.trade_source:
            self.trade_source.stop()
        if self._signal_worker:
            self._signal_worker.join(timeout=5)
        self.stop_account_polling()
        logger.info("[OrderManager] ⏹ 已停止")

    def place_order(self, symbol: str, direction: str, order_type: str,
                    price: float, qty: int, **kwargs) -> Optional[str]:
        """
        下单（v3.6.0 原有方法）
        优先使用 trade_source，否则内部模拟
        """
        if self.trade_source:
            order_id = self.trade_source.place_order(
                symbol, direction, order_type, price, qty, **kwargs
            )
        else:
            order_id = f"OM_{int(time.time()*1000)}"
            order = OrderData(
                order_id=order_id, symbol=symbol, direction=direction.upper(),
                order_type=order_type.upper(), price=price, total_qty=qty,
                create_time=time.time(), update_time=time.time(), ext=kwargs,
            )
            with self._lock:
                self._orders[order_id] = {
                    'order_id': order_id, 'symbol': symbol,
                    'direction': direction.upper(), 'price': price,
                    'volume': qty, 'status': 'SUBMITTED',
                    'create_time': order.create_time,
                }
        logger.info(f"[OrderManager] 📤 {order_id} {direction} {symbol} {qty}@{price:.2f}")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        """撤单（v3.6.0 原有方法）"""
        if self.trade_source:
            return self.trade_source.cancel_order(order_id)
        with self._lock:
            order = self._orders.get(order_id)
            if order and order['status'] in ('SUBMITTED', 'PARTIAL_FILLED'):
                order['status'] = 'CANCELLED_ALL'
                order['update_time'] = time.time()
                logger.info(f"[OrderManager] ⏹ 撤单: {order_id}")
                return True
        return False

    def get_order(self, order_id: str) -> Optional[dict]:
        """查询订单"""
        if self.trade_source:
            order = self.trade_source.get_order(order_id)
            if order:
                return {
                    'order_id': order.order_id, 'symbol': order.symbol,
                    'direction': order.direction, 'price': order.price,
                    'volume': order.total_qty, 'traded': order.traded_qty,
                    'status': order.status,
                }
        with self._lock:
            return self._orders.get(order_id)

    def get_all_orders(self) -> List[dict]:
        """获取全部订单"""
        if self.trade_source:
            return []
        with self._lock:
            return list(self._orders.values())

    def get_trades(self) -> List[TradeRecord]:
        """获取全部成交"""
        if self.trade_source:
            return self.trade_source.get_trades()
        with self._lock:
            return list(self._trades)

    def _on_trade_callback(self, trade: TradeRecord) -> None:
        """成交回调处理（v3.6.0 原有方法，v3.8.0 增强）"""
        with self._lock:
            self._trades.append(trade)
            # 更新内部订单状态
            if trade.order_id in self._orders:
                order = self._orders[trade.order_id]
                # 尝试从 trade_source 获取完整订单信息
                src_order = None
                if self.trade_source:
                    src_order = self.trade_source.get_order(trade.order_id)

                if src_order:
                    order['traded'] = src_order.traded_qty
                    total = src_order.total_qty
                    order['status'] = 'FILLED_ALL' if src_order.traded_qty >= total else 'PARTIAL_FILLED'
                else:
                    order['traded'] = order.get('traded', 0) + trade.qty
                    total = order.get('volume', trade.qty)
                    if order['traded'] >= total:
                        order['status'] = 'FILLED_ALL'
                    else:
                        order['status'] = 'PARTIAL_FILLED'
                order['update_time'] = trade.trade_time

        logger.info(f"[OrderManager] ✅ 成交: {trade.trade_id} {trade.symbol} "
                     f"{trade.qty}@{trade.price:.4f}")

        # v3.8.0：触发 fill 回调
        self._dispatch_fill_callback(trade)

    def _on_order_callback(self, order: OrderData) -> None:
        """订单状态回调（v3.8.0 新增）"""
        logger.debug(f"[OrderManager] 订单状态: {order.order_id} {order.status}")

    def _dispatch_fill_callback(self, trade: TradeRecord) -> None:
        """分发 fill 回调（v3.8.0 新增）"""
        # 查找对应的 order 信息
        order_info = None
        with self._lock:
            for oid, odata in self._orders.items():
                if oid == trade.order_id:
                    order_info = odata
                    break

        if not order_info:
            return

        # 计算 PnL（简化版）
        pnl = 0.0
        direction = order_info.get('direction', '')
        entry_price = order_info.get('price', 0)
        if direction == 'SELL' and entry_price > 0:
            pnl = (trade.price - entry_price) * trade.qty
        elif direction == 'BUY' and entry_price > 0:
            pnl = (trade.price - entry_price) * trade.qty

        # 更新策略统计
        strategy_name = order_info.get('strategy_name', '')
        if strategy_name:
            self._strategy_stats[strategy_name]['filled'] += 1
            self._strategy_stats[strategy_name]['pnl'] += pnl

        # 调用注册的回调
        symbol = trade.symbol
        for key, callback in list(self._fill_callbacks.items()):
            if key.endswith(symbol) or key == symbol:
                try:
                    callback(trade, pnl)
                except Exception as e:
                    logger.error(f"[OrderManager] fill callback error: {e}")

    # ==================== v3.8.0 新增方法 ====================

    def submit_signal(self, symbol: str, direction: str,
                      price: float, volume: int,
                      offset: str = 'OPEN',
                      strategy_name: str = "",
                      user_id: str = "SYSTEM",
                      auto_size: bool = False,
                      **kwargs) -> str:
        """
        提交交易信号（v3.8.0 智能路由入口）
        这是 v3.8.0 的核心入口方法，所有下单都经过此处

        :param symbol: 标的代码
        :param direction: LONG / SHORT
        :param price: 价格
        :param volume: 数量
        :param offset: OPEN / CLOSE
        :param strategy_name: 策略名称
        :param user_id: 用户ID
        :param auto_size: 是否自动计算仓位
        :return: signal_id
        """
        signal_id = f"SIG_{user_id}_{symbol}_{direction}_{datetime.now():%H%M%S%f}"

        signal = {
            'signal_id': signal_id,
            'symbol': symbol,
            'direction': direction,
            'price': price,
            'volume': volume,
            'offset': offset,
            'strategy_name': strategy_name,
            'user_id': user_id,
            'auto_size': auto_size,
            'submitted_at': datetime.now().isoformat(),
            'status': 'PENDING',
        }
        signal.update(kwargs)

        # 放入信号队列（异步处理）
        self._signal_queue.put(signal)

        # 更新统计
        self._user_stats[user_id]['sent'] += 1
        if strategy_name:
            self._strategy_stats[strategy_name]['sent'] += 1

        logger.info(
            f"[Order] 📤 {signal_id} | {direction} {symbol} "
            f"{volume}@{price:.2f} offset={offset} user={user_id} "
            f"strategy={strategy_name}"
        )
        return signal_id

    def _signal_loop(self) -> None:
        """信号处理循环（v3.8.0 新增）"""
        while self._signal_running:
            try:
                signal = self._signal_queue.get(timeout=1.0)
                self._process_signal(signal)
            except Empty:
                continue
            except Exception as e:
                logger.error(f"[Order] 信号处理异常: {e}")

    def _process_signal(self, signal: dict) -> None:
        """处理单个信号（v3.8.0 新增）"""
        symbol = signal['symbol']
        direction = signal['direction']
        price = signal['price']
        volume = signal['volume']
        offset = signal.get('offset', 'OPEN')
        strategy_name = signal.get('strategy_name', '')
        user_id = signal.get('user_id', 'SYSTEM')

        # 路由到对应市场
        market = self._detect_market(symbol)
        gw = self.gateways.get(market)

        if gw is None:
            logger.error(f"[Order] ❌ 无可用网关: {market} for {symbol}")
            self.on_order_reject(signal['signal_id'], "无可用交易网关")
            return

        # 映射方向
        futu_direction = self._map_direction(direction, offset)

        # 通过网关下单
        try:
            if hasattr(gw, 'send_order'):
                order_id = gw.send_order(
                    symbol=symbol, price=price, volume=volume,
                    direction=direction, offset=offset,
                    order_type='LIMIT',
                )
                if order_id:
                    signal['status'] = 'SUBMITTED'
                    signal['order_id'] = order_id
                    # 存储信号信息
                    with self._lock:
                        self._orders[order_id] = {
                            'order_id': order_id,
                            'symbol': symbol,
                            'direction': direction,
                            'price': price,
                            'volume': volume,
                            'traded': 0,
                            'status': 'SUBMITTED',
                            'strategy_name': strategy_name,
                            'user_id': user_id,
                            'signal_id': signal['signal_id'],
                        }
                    logger.info(f"[Order] ✅ 路由成功: {signal['signal_id']} → {order_id} via {market}")
                else:
                    self.on_order_reject(signal['signal_id'], "网关返回空 order_id")
            else:
                # 降级：使用 trade_source
                if self.trade_source:
                    order_id = self.trade_source.place_order(
                        symbol, futu_direction, 'LIMIT', price, volume,
                        strategy_name=strategy_name, user_id=user_id,
                    )
                    if order_id:
                        signal['status'] = 'SUBMITTED'
                        signal['order_id'] = order_id
                    else:
                        self.on_order_reject(signal['signal_id'], "trade_source 返回空")
                else:
                    self.on_order_reject(signal['signal_id'], "无可用下单通道")
        except Exception as e:
            logger.error(f"[Order] 下单异常: {e}\n{traceback.format_exc()}")
            self.on_order_reject(signal['signal_id'], str(e))

    def on_order_fill(self, signal_id: str, fill_price: float,
                        fill_volume: int, commission: float = 0.0) -> None:
        """订单成交通知（v3.8.0 新增）"""
        with self._lock:
            order = None
            for oid, odata in self._orders.items():
                if odata.get('signal_id') == signal_id:
                    order = odata
                    break

        if not order:
            logger.warning(f"[Order] 未知成交信号: {signal_id}")
            return

        order['status'] = 'FILLED'
        order['fill_price'] = fill_price
        order['fill_volume'] = fill_volume
        order['commission'] = commission
        order['filled_at'] = datetime.now().isoformat()

        user_id = order.get('user_id', 'SYSTEM')
        strategy_name = order.get('strategy_name', '')

        self._user_stats[user_id]['filled'] += 1
        if strategy_name:
            self._strategy_stats[strategy_name]['filled'] += 1

        pnl = self._calc_pnl(order, fill_price, fill_volume)

        logger.info(
            f"[Order] ✅ FILL {signal_id} {fill_volume}@{fill_price:.2f} "
            f"PnL={pnl:+.2f} strategy={strategy_name}"
        )

    def on_order_reject(self, signal_id: str, reason: str = "") -> None:
        """订单拒绝通知（v3.8.0 新增）"""
        user_id = 'SYSTEM'
        # 尝试查找 user_id
        with self._lock:
            for oid, odata in self._orders.items():
                if odata.get('signal_id') == signal_id:
                    user_id = odata.get('user_id', 'SYSTEM')
                    odata['status'] = 'REJECTED'
                    break

        self._user_stats[user_id]['rejected'] += 1
        logger.warning(f"[Order] 🚫 REJECT {signal_id}: {reason}")

    def on_order_cancel(self, signal_id: str, reason: str = "") -> None:
        """订单取消通知（v3.8.0 新增）"""
        user_id = 'SYSTEM'
        with self._lock:
            for oid, odata in self._orders.items():
                if odata.get('signal_id') == signal_id:
                    user_id = odata.get('user_id', 'SYSTEM')
                    odata['status'] = 'CANCELLED'
                    break

        self._user_stats[user_id]['cancelled'] += 1
        logger.info(f"[Order] ⏹ CANCEL {signal_id}: {reason}")

    def update_equity(self, user_id: str, new_value: float) -> None:
        """更新用户权益（v3.8.0 新增）"""
        old = self._equity.get(user_id, {}).get('current_value', new_value)
        self._equity[user_id] = {
            'last_value': old,
            'current_value': new_value,
            'updated_at': datetime.now().isoformat(),
        }
        if abs(new_value - old) > 0.01:
            logger.info(f"[Order] 权益变更 {user_id}: {old:,.2f} → {new_value:,.2f}")

    def get_fill_rate(self, strategy_name: str = "") -> float:
        """获取填充率（v3.8.0 新增）"""
        if strategy_name:
            stats = self._strategy_stats.get(strategy_name, {})
            sent = stats.get('sent', 0)
            filled = stats.get('filled', 0)
            return (filled / sent) if sent > 0 else 1.0
        total_sent = sum(s['sent'] for s in self._strategy_stats.values())
        total_filled = sum(s['filled'] for s in self._strategy_stats.values())
        return (total_filled / total_sent) if total_sent > 0 else 1.0

    def get_user_stats(self, user_id: str) -> dict:
        """获取用户统计（v3.8.0 新增）"""
        return dict(self._user_stats.get(user_id, {
            'sent': 0, 'filled': 0, 'rejected': 0, 'cancelled': 0
        }))

    def get_strategy_stats(self, strategy_name: str) -> dict:
        """获取策略统计（v3.8.0 新增）"""
        return dict(self._strategy_stats.get(strategy_name, {
            'sent': 0, 'filled': 0, 'pnl': 0.0
        }))

    def get_net_qty(self, symbol: str, user_id: str = "SYSTEM") -> int:
        """获取净持仓（v3.8.0 新增）"""
        net = 0
        with self._lock:
            for order in self._orders.values():
                if order.get('symbol') != symbol:
                    continue
                if order.get('user_id') != user_id:
                    continue
                if order.get('status') not in ('FILLED', 'FILLED_ALL', 'PARTIAL_FILLED'):
                    continue
                qty = order.get('fill_volume', order.get('traded', order.get('volume', 0)))
                direction = order.get('direction', 'LONG')
                if direction in ('LONG', 'BUY'):
                    net += qty
                elif direction in ('SHORT', 'SELL'):
                    net -= qty
        return net

    def register_fill_callback(self, symbol: str, callback: Callable,
                                user_id: str = "SYSTEM") -> None:
        """注册成交回调（v3.8.0 新增）"""
        key = f"{user_id}:{symbol}"
        self._fill_callbacks[key] = callback
        logger.info(f"[Order] 🔗 注册回调: {key}")

    def reset_daily(self) -> None:
        """每日统计重置（v3.8.0 新增）"""
        self._user_stats.clear()
        self._strategy_stats.clear()
        logger.info("[Order] 🔄 每日统计已重置")

    def get_status(self) -> dict:
        """获取状态摘要（v3.8.0 新增）"""
        with self._lock:
            total_orders = len(self._orders)
            active = sum(1 for o in self._orders.values()
                         if o.get('status') in ('PENDING', 'SUBMITTED', 'PARTIAL_FILLED'))
            total_trades = len(self._trades)

        return {
            'total_orders': total_orders,
            'active_orders': active,
            'total_trades': total_trades,
            'users': len(self._user_stats),
            'fill_rate': self.get_fill_rate(),
            'callbacks': len(self._fill_callbacks),
            'equity_users': len(self._equity),
            'trade_source': type(self.trade_source).__name__ if self.trade_source else None,
            'gateways': list(self.gateways.keys()),
        }

    # ==================== 内部辅助方法 ====================

    def _route_signal(self, signal: dict) -> None:
        """路由信号到对应市场（v3.8.0 新增）"""
        symbol = signal['symbol']
        market = self._detect_market(symbol)
        gw = self.gateways.get(market)
        if not gw:
            logger.error(f"[Order] 无可用网关: {market} for {symbol}")
            self.on_order_reject(signal['signal_id'], f"无可用网关: {market}")
            return
        logger.debug(f"[Order] 路由 → {market}: {signal['signal_id']}")

    def _detect_market(self, symbol: str) -> str:
        """自动识别市场（v3.8.0 新增）"""
        s = str(symbol).upper()
        if s.isdigit() and len(s) == 5:
            return "HK"
        if s.startswith("HK."):
            return "HK"
        if s.startswith("US."):
            return "US"
        # 纯字母 → US
        if s.isalpha():
            return "US"
        # 纯数字 → HK
        if s.isdigit():
            return "HK"
        return "US"

    def _map_direction(self, direction: str, offset: str) -> str:
        """映射方向到 futu 格式"""
        d = direction.upper()
        o = offset.upper()
        if d == 'LONG' and o == 'OPEN':
            return 'BUY'
        elif d == 'SHORT' and o == 'OPEN':
            return 'SELL_SHORT'
        elif d == 'LONG' and o == 'CLOSE':
            return 'BUY_BACK'
        elif d == 'SHORT' and o == 'CLOSE':
            return 'SELL'
        return d

    def _calc_pnl(self, order: dict, fill_price: float, fill_volume: int) -> float:
        """计算盈亏（v3.8.0 新增，简化版）"""
        direction = order.get('direction', 'LONG')
        offset = order.get('offset', 'OPEN')
        entry = order.get('price', 0)
        if offset == 'OPEN':
            return 0.0  # 开仓不计算 PnL
        if direction == 'LONG':
            return (fill_price - entry) * fill_volume
        elif direction == 'SHORT':
            return (entry - fill_price) * fill_volume
        return 0.0

    def _format_code(self, symbol: str) -> str:
        """格式化代码"""
        if "." in symbol:
            return symbol
        if symbol.isdigit() and len(symbol) == 5:
            return f"HK.{symbol}"
        return f"US.{symbol}"

    # ==================== v3.8.0 兼容接口 ====================

    def set_position_manager(self, position_manager) -> None:
        """设置持仓管理器引用"""
        self.position_manager = position_manager
        logger.info("[OrderManager] 🔗 PositionManager 已连接")

    def get_gateway_for_market(self, market: str):
        """获取指定市场的网关"""
        return self.gateways.get(market)

    def add_gateway(self, market: str, gateway) -> None:
        """动态添加网关"""
        self.gateways[market] = gateway
        logger.info(f"[OrderManager] ➕ 网关已添加: {market}")

    def remove_gateway(self, market: str) -> None:
        """移除网关"""
        if market in self.gateways:
            del self.gateways[market]
            logger.info(f"[OrderManager] ➖ 网关已移除: {market}")

    def __repr__(self) -> str:
        s = self.get_status()
        return (
            f"OrderManager(v3.8.0, orders={s['total_orders']}, "
            f"active={s['active_orders']}, trades={s['total_trades']}, "
            f"fill_rate={s['fill_rate']:.1%}, users={s['users']})"
        )


# ==================== 工厂函数 ====================

def create_order_manager(config: dict = None, **kwargs) -> OrderManager:
    """
    工厂函数：根据配置创建 OrderManager
    v3.8.0 新增
    """
    config = config or {}
    gateways = kwargs.get('gateways', config.get('gateways', {}))
    equity = kwargs.get('account_equity', config.get('account_equity', 100000.0))

    # 根据环境选择 trade_source
    env = config.get('trade_env', 'SIMULATE').upper()
    if env == 'REAL':
        trade_source = LiveTradeSource(poll_interval=config.get('poll_interval', 2.0))
        logger.info("[Factory] 创建 LiveTradeSource（实盘模式）")
    else:
        trade_source = SimTradeSource(poll_interval=config.get('poll_interval', 2.0))
        logger.info("[Factory] 创建 SimTradeSource（模拟模式）")

    om = OrderManager(
        gateways=gateways,
        account_equity=equity,
        trade_source=trade_source,
        poll_interval=config.get('poll_interval', 60.0),
    )
    return om


# ==================== 向后兼容别名 ====================

# v3.6.0 中可能使用的旧名称
OrderMgr = OrderManager
SimSource = SimTradeSource
LiveSource = LiveTradeSource


if __name__ == "__main__":
    # 快速自检
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)-15s | %(levelname)-5s : %(message)s")

    om = create_order_manager({'trade_env': 'SIMULATE', 'account_equity': 50000})
    om.start()

    # 测试下单
    sid = om.submit_signal(symbol="AAPL", direction="LONG", price=150.0, volume=10,
                            strategy_name="TestStrategy", user_id="test_user")
    print(f"Signal ID: {sid}")

    # 等待模拟成交
    time.sleep(5)

    # 查看状态
    status = om.get_status()
    print(f"Status: {json.dumps(status, indent=2)}")

    # 查看统计
    print(f"User stats: {om.get_user_stats('test_user')}")
    print(f"Fill rate: {om.get_fill_rate():.1%}")

    om.stop()
    print("✅ OrderManager v3.8.0 自检完成")
