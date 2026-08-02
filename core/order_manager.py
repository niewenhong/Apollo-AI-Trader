"""
core/order_manager.py - Apollo Order Manager
Intelligent signal queue + rate control + smart pricing + order polling + position management
"""
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Dict, Callable, List, Any
import logging

logger = logging.getLogger("OrderManager")


# ==================== Data Structures ====================
@dataclass(order=True)
class SignalItem:
    priority: float = 0.0
    timestamp: float = 0.0
    symbol: str = ''
    direction: str = ''        # 'LONG' / 'SHORT'
    price: float = 0.0
    volume: int = 0
    offset: str = 'OPEN'       # 'OPEN' / 'CLOSE'
    strategy_name: str = ''
    extra: dict = field(default_factory=dict)


@dataclass
class ActiveOrder:
    order_id: str
    symbol: str
    direction: str
    volume: int
    price: float
    offset: str
    status: str = 'PENDING'
    filled_qty: int = 0
    filled_price: float = 0.0
    create_time: float = 0.0
    last_query: float = 0.0


@dataclass
class Position:
    symbol: str
    long_qty: int = 0
    short_qty: int = 0
    avg_long_price: float = 0.0
    avg_short_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def net_qty(self) -> int:
        return self.long_qty - self.short_qty

    @property
    def gross_qty(self) -> int:
        return self.long_qty + self.short_qty

    def update_on_fill(self, direction: str, qty: int, price: float):
        if direction == 'LONG':
            if self.short_qty > 0 and qty <= self.short_qty:
                self.realized_pnl += (self.avg_short_price - price) * qty
                self.short_qty -= qty
                if self.short_qty == 0:
                    self.avg_short_price = 0.0
            elif self.short_qty > 0:
                close_qty = self.short_qty
                self.realized_pnl += (self.avg_short_price - price) * close_qty
                self.short_qty = 0
                self.avg_short_ = 0.0
                self.avg_short_price = 0.0
                remain = qty - close_qty
                total = self.avg_long_price * self.long_qty + price * remain
                self.long_qty += remain
                self.avg_long_price = total / self.long_qty if self.long_qty > 0 else 0
            else:
                total = self.avg_long_price * self.long_qty + price * qty
                self.long_qty += qty
                self.avg_long_price = total / self.long_qty if self.long_qty > 0 else 0
        else:  # SHORT
            if self.long_qty > 0 and qty <= self.long_qty:
                self.realized_pnl += (price - self.avg_long_price) * qty
                self.long_qty -= qty
                if self.long_qty == 0:
                    self.avg_long_price = 0.0
            elif self.long_qty > 0:
                close_qty = self.long_qty
                self.realized_pnl += (price - self.avg_long_price) * close_qty
                self.long_qty = 0
                self.avg_long_price = 0.0
                remain = qty - close_qty
                total = self.avg_short_price * self.short_qty + price * remain
                self.short_qty += remain
                self.avg_short_price = total / self.short_qty if self.short_qty > 0 else 0
            else:
                total = self.avg_short_price * self.short_qty + price * qty
                self.short_qty += qty
                self.avg_short_price = total / self.short_qty if self.short_qty > 0 else 0


# ==================== Rate Controller ====================
class RateController:
    """Futu limit: 15 requests / 30 sec. We use conservative 14/30s, gap 0.1s."""

    def __init__(self, max_requests=14, window=30.0, min_interval=0.1):
        self.max_requests = max_requests
        self.window = window
        self.min_interval = min_interval
        self._timestamps = deque()
        self._last_send = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.time()
            while self._timestamps and now - self._timestamps[0] >= self.window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_requests:
                wait = self.window - (now - self._timestamps[0]) + 0.2
                if wait > 0:
                    time.sleep(wait)
                now = time.time()
            gap = self.min_interval - (now - self._last_send)
            if gap > 0:
                time.sleep(gap)
            self._last_send = time.time()
            self._timestamps.append(self._last_send)


# ==================== Smart Pricing ====================
class SmartPricing:
    def __init__(self, slippage_rate=0.001):
        self.slippage_rate = slippage_rate

    def adjust(self, direction: str, original_price: float,
               bid: float, ask: float) -> float:
        if bid <= 0 or ask <= 0 or ask <= bid:
            return round(original_price, 2)
        if direction == 'LONG':
            target = ask * (1 + self.slippage_rate)
            return round(max(original_price, target), 2)
        else:
            target = bid * (1 - self.slippage_rate)
            return round(min(original_price, target), 2)


# ==================== Position Sizer ====================
class PositionSizer:
    def __init__(self, account_equity=100000.0, max_risk_per_trade=0.02,
                 max_position_ratio=0.25, atr_multiplier=2.0):
        self.account_equity = account_equity
        self.max_risk_per_trade = max_risk_per_trade
        self.max_position_ratio = max_position_ratio
        self.atr_multiplier = atr_multiplier

    def calculate(self, entry_price: float, atr: float = None,
                  signal_strength: float = 1.0) -> int:
        if atr is None or atr <= 0:
            atr = entry_price * 0.01
        risk_amount = self.account_equity * self.max_risk_per_trade * signal_strength
        stop_distance = self.atr_multiplier * atr
        if stop_distance <= 0:
            return 0
        shares_by_risk = int(risk_amount / stop_distance)
        max_shares = int(self.account_equity * self.max_position_ratio / entry_price)
        return max(1, min(shares_by_risk, max_shares))


# ==================== OrderManager ====================
class OrderManager:
    def __init__(self, gateways: Dict[str, Any], query_interval=2.0,
                 account_equity=100000.0):
        self.gateways = gateways
        self.rate_ctrl = RateController()
        self.pricing = SmartPricing()
        self.sizer = PositionSizer(account_equity=account_equity)
        self._signal_queue: List[SignalItem] = []
        self._signal_lock = threading.Lock()
        self._active_orders: Dict[str, ActiveOrder] = {}
        self._order_lock = threading.Lock()
        self._positions: Dict[str, Position] = {}
        self._pos_lock = threading.Lock()
        self._callbacks: Dict[str, Callable] = {}
        self._query_interval = query_interval
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self._process_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._process_thread.start()
        logger.info("[OrderManager] started")

    def stop(self):
        self._running = False

    # ---------- submit signal ----------
    def submit_signal(self, symbol: str, direction: str, price: float,
                      volume: int = 0, offset: str = 'OPEN',
                      strategy_name: str = '', priority: float = 0.0,
                      auto_size: bool = True, atr: float = None,
                      signal_strength: float = 1.0):
        if volume <= 0 and auto_size:
            volume = self.sizer.calculate(price, atr=atr, signal_strength=signal_strength)
            logger.info(f"[Sizer] {symbol} {direction} auto size = {volume}")
        with self._signal_lock:
            self._signal_queue.append(SignalItem(
                priority=priority,
                timestamp=time.time(),
                symbol=symbol,
                direction=direction,
                price=price,
                volume=volume,
                offset=offset,
                strategy_name=strategy_name
            ))
        logger.info(f"[Signal] queued: {symbol} {direction} {volume}@{price} ({strategy_name})")

    # ---------- process loop ----------
    def _process_loop(self):
        while self._running:
            signal = None
            with self._signal_lock:
                if self._signal_queue:
                    self._signal_queue.sort(key=lambda x: (-x.priority, x.timestamp))
                    signal = self._signal_queue.pop(0)
            if signal:
                self._execute_signal(signal)
            else:
                time.sleep(0.1)

    def _execute_signal(self, signal: SignalItem):
        gw = self._select_gateway(signal.symbol)
        if not gw:
            logger.error(f"[OrderManager] no gateway for {signal.symbol}")
            return
        bid, ask = self._get_market_depth(gw, signal.symbol)
        adj_price = self.pricing.adjust(signal.direction, signal.price, bid, ask)
        self.rate_ctrl.acquire()
        try:
            from vnpy.trader.object import OrderRequest, Direction, Offset, OrderType
            req = OrderRequest(
                symbol=signal.symbol,
                exchange='SMART',
                direction=Direction.LONG if signal.direction == 'LONG' else Direction.SHORT,
                type=OrderType.LIMIT,
                volume=signal.volume,
                price=adj_price,
                offset=Offset.OPEN if signal.offset == 'OPEN' else Offset.CLOSE,
                reference=f"OM_{signal.strategy_name}"
            )
            order_id = gw.send_order(req)
            if order_id:
                with self._order_lock:
                    self._active_orders[order_id] = ActiveOrder(
                        order_id=order_id,
                        symbol=signal.symbol,
                        direction=signal.direction,
                        volume=signal.volume,
                        price=adj_price,
                        offset=signal.offset,
                        create_time=time.time()
                    )
                logger.info(f"[OrderManager] ORDER SENT: {order_id} "
                           f"{signal.symbol} {signal.direction} {signal.volume}@{adj_price}")
            else:
                logger.error(f"[OrderManager] order failed: {signal.symbol}")
        except Exception as e:
            logger.error(f"[OrderManager] send exception: {e}")

    def _select_gateway(self, symbol: str):
        if '.HK' in symbol or (symbol.isdigit() and len(symbol) == 5):
            return self.gateways.get('FUTU_HK')
        return self.gateways.get('FUTU_US')

    def _get_market_depth(self, gw, symbol):
        # Try to read from gateway cache; fallback placeholder
        try:
            if hasattr(gw, 'quote_ctx') and gw.quote_ctx:
                pass  # actual depth retrieval depends on your market data bus
        except Exception:
            pass
        return 0.0, 999999.0

    # ---------- polling loop ----------
    def _poll_loop(self):
        while self._running:
            time.sleep(self._query_interval)
            self._poll_orders()

    def _poll_orders(self):
        with self._order_lock:
            if not self._active_orders:
                return
            snapshot = list(self._active_orders.values())
        for gw_key, gw in self.gateways.items():
            method = getattr(gw, 'get_order_list', None)
            if method is None:
                continue
            try:
                ret, data = method(status_filter='SUBSCRIBED')
                if ret != 0 or not data:
                    continue
                order_map = {o.get('order_id'): o for o in data.get('order_list', [])}
                for ao in snapshot:
                    raw = order_map.get(ao.order_id)
                    if not raw:
                        continue
                    new_status = raw.get('order_status', '')
                    new_filled = int(raw.get('dealt_qty', 0))
                    new_price = float(raw.get('dealt_avg_price', 0))
                    if (new_status != ao.status or
                        new_filled != ao.filled_qty or
                        abs(new_price - ao.filled_price) > 0.001):
                        self._on_order_update(ao, new_status, new_filled, new_price)
            except Exception as e:
                logger.error(f"[OrderManager] poll error on {gw_key}: {e}")

    def _on_order_update(self, ao: ActiveOrder, new_status: str,
                         new_filled: int, new_price: float):
        old_filled = ao.filled_qty
        ao.status = new_status
        ao.filled_qty = new_filled
        ao.filled_price = new_price
        ao.last_query = time.time()
        added = new_filled - old_filled
        if added > 0:
            with self._pos_lock:
                pos = self._positions.get(ao.symbol)
                if not pos:
                    pos = Position(symbol=ao.symbol)
                    self._positions[ao.symbol] = pos
                pos.update_on_fill(ao.direction, added, new_price)
            logger.info(f"[Position] {ao.symbol} {ao.direction} +{added}@{new_price} "
                       f"=> net={pos.net_qty}")
            cb = self._callbacks.get(ao.symbol)
            if cb:
                try:
                    cb(ao.symbol, ao.direction, added, new_price, pos.net_qty)
                except Exception as e:
                    logger.error(f"[OrderManager] callback error: {e}")
        logger.info(f"[Order] {ao.order_id} {new_status} filled {new_filled}/{ao.volume}")
        if new_status in ('ALL_TRADED', 'CANCELLED', 'REJECTED', 'DELETED'):
            with self._order_lock:
                self._active_orders.pop(ao.order_id, None)

    # ---------- query API ----------
    def get_position(self, symbol: str) -> Optional[Position]:
        with self._pos_lock:
            return self._positions.get(symbol)

    def get_net_qty(self, symbol: str) -> int:
        with self._pos_lock:
            pos = self._positions.get(symbol)
            return pos.net_qty if pos else 0

    def get_all_positions(self) -> Dict[str, Position]:
        with self._pos_lock:
            return dict(self._positions)

    def register_fill_callback(self, symbol: str, callback: Callable):
        self._callbacks[symbol] = callback

    def cancel_order(self, order_id: str) -> bool:
        with self._order_lock:
            ao = self._active_orders.get(order_id)
            if not ao:
                return False
            gw = self._select_gateway(ao.symbol)
            if gw and hasattr(gw, 'cancel_order'):
                try:
                    gw.cancel_order(order_id)
                    logger.info(f"[Cancel] {order_id}")
                    return True
                except Exception as e:
                    logger.error(f"[Cancel] failed: {e}")
        return False

    def cancel_all_for_symbol(self, symbol: str):
        with self._order_lock:
            targets = [ao.order_id for ao in self._active_orders.values()
                      if ao.symbol == symbol]
        for oid in targets:
            self.cancel_order(oid)