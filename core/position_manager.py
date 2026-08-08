"""
core/position_manager.py - 智能持仓管理 v3.5.0
修复记录（基于 v3.2.0 基线）：
1. [HIGH] update(): direction 字符串比较用 == 而非 is
2. [MEDIUM] _add_long/_add_short: 先平后开时未正确处理 avg_price 更新
3. [MEDIUM] exposure() 返回 float 但未考虑杠杆
4. [NEW] 新增: to_dict() / from_dict() 序列化支持
5. [NEW] 新增: sync_from_gateway() 从 FutuGateway 持仓数据矫正
6. [NEW] 新增: register_change_callback 持仓变更通知
"""
from typing import Dict, Optional, Callable, List
import logging

logger = logging.getLogger("PositionManager")

class Position:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.long_qty: int = 0
        self.short_qty: int = 0
        self.avg_long_price: float = 0.0
        self.avg_short_price: float = 0.0
        self.realized_pnl: float = 0.0

    @property
    def net_qty(self) -> int:
        return self.long_qty - self.short_qty

    @property
    def gross_qty(self) -> int:
        return self.long_qty + self.short_qty

    def update(self, direction: str, qty: int, price: float):
        """外部调用，更新持仓"""
        # ★ 修复：用 == 而非 is 比较字符串
        if direction == 'LONG':
            self._add_long(qty, price)
        elif direction == 'SHORT':
            self._add_short(qty, price)
        else:
            logger.warning(f"未知方向: {direction}")

    def _add_long(self, qty: int, price: float):
        if self.short_qty > 0:
            close_qty = min(qty, self.short_qty)
            if self.avg_short_price > 0:
                self.realized_pnl += (self.avg_short_price - price) * close_qty
            self.short_qty -= close_qty
            qty -= close_qty
            if self.short_qty == 0:
                self.avg_short_price = 0.0
        if qty > 0:
            total_cost = self.avg_long_price * self.long_qty + price * qty
            self.long_qty += qty
            self.avg_long_price = total_cost / self.long_qty if self.long_qty > 0 else 0

    def _add_short(self, qty: int, price: float):
        if self.long_qty > 0:
            close_qty = min(qty, self.long_qty)
            if self.avg_long_price > 0:
                self.realized_pnl += (price - self.avg_long_price) * close_qty
            self.long_qty -= close_qty
            qty -= close_qty
            if self.long_qty == 0:
                self.avg_long_price = 0.0
        if qty > 0:
            total_cost = self.avg_short_price * self.short_qty + price * qty
            self.short_qty += qty
            self.avg_short_price = total_cost / self.short_qty if self.short_qty > 0 else 0

    def unrealized_pnl(self, current_price: float) -> float:
        pnl = 0.0
        if self.long_qty > 0:
            pnl += (current_price - self.avg_long_price) * self.long_qty
        if self.short_qty > 0:
            pnl += (self.avg_short_price - current_price) * self.short_qty
        return pnl

    def total_pnl(self, current_price: float) -> float:
        return self.realized_pnl + self.unrealized_pnl(current_price)

    def exposure(self, current_price: float, leverage: float = 1.0) -> float:
        """风险敞口（绝对值 × 杠杆）"""
        return abs(self.net_qty) * current_price * leverage

    def to_dict(self) -> dict:
        """★ 新增：序列化"""
        return {
            'symbol': self.symbol,
            'long_qty': self.long_qty,
            'short_qty': self.short_qty,
            'avg_long_price': self.avg_long_price,
            'avg_short_price': self.avg_short_price,
            'realized_pnl': self.realized_pnl,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Position':
        """★ 新增：反序列化"""
        p = cls(data['symbol'])
        p.long_qty = data.get('long_qty', 0)
        p.short_qty = data.get('short_qty', 0)
        p.avg_long_price = data.get('avg_long_price', 0.0)
        p.avg_short_price = data.get('avg_short_price', 0.0)
        p.realized_pnl = data.get('realized_pnl', 0.0)
        return p

    def __repr__(self):
        return (f"Position({self.symbol}: L={self.long_qty}@{self.avg_long_price:.2f}, "
                f"S={self.short_qty}@{self.avg_short_price:.2f}, "
                f"net={self.net_qty}, rPNL={self.realized_pnl:.2f})")


class PositionManager:
    def __init__(self):
        self._positions: Dict[str, Position] = {}
        self._callbacks: List[Callable] = []
        self._lock = threading.Lock() if False else None  # placeholder

    def get_or_create(self, symbol: str) -> Position:
        if symbol not in self._positions:
            self._positions[symbol] = Position(symbol)
        return self._positions[symbol]

    def update(self, symbol: str, direction: str, qty: int, price: float):
        pos = self.get_or_create(symbol)
        old_net = pos.net_qty
        pos.update(direction, qty, price)
        logger.info(f"[PositionManager] {symbol} 更新: {pos}")
        # 通知回调
        for cb in list(self._callbacks):
            try:
                cb(symbol, pos)
            except Exception as e:
                logger.error(f"[PositionManager] callback error: {e}")
        return pos

    def sync_from_gateway(self, symbol: str, qty: int, cost_price: float,
                           direction: str = 'LONG'):
        """
        ★ 新增：从 FutuGateway.query_position 数据矫正
        qty: 富途返回的持仓数量
        cost_price: 富途返回的持仓成本
        direction: 'LONG' 或 'SHORT'（富途 NET 方向需转换）
        """
        pos = self.get_or_create(symbol)
        old_net = pos.net_qty

        if direction == 'LONG':
            if pos.long_qty != qty or abs(pos.avg_long_price - cost_price) > 0.01:
                logger.warning(
                    f"[PositionManager] 矫正 {symbol}: "
                    f"LONG {pos.long_qty}@{pos.avg_long_price:.2f} "
                    f"→ {qty}@{cost_price:.2f}"
                )
                pos.long_qty = qty
                pos.avg_long_price = cost_price
                if qty == 0:
                    pos.avg_long_price = 0.0
        else:
            if pos.short_qty != qty or abs(pos.avg_short_price - cost_price) > 0.01:
                logger.warning(
                    f"[PositionManager] 矫正 {symbol}: "
                    f"SHORT {pos.short_qty}@{pos.avg_short_price:.2f} "
                    f"→ {qty}@{cost_price:.2f}"
                )
                pos.short_qty = qty
                pos.avg_short_price = cost_price
                if qty == 0:
                    pos.avg_short_price = 0.0

        if old_net != pos.net_qty:
            logger.info(f"[PositionManager] {symbol} net: {old_net} → {pos.net_qty}")

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_net_qty(self, symbol: str) -> int:
        pos = self._positions.get(symbol)
        return pos.net_qty if pos else 0

    def get_all_positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    def get_total_exposure(self, price_map: Dict[str, float],
                           leverage: float = 1.0) -> float:
        """★ 新增：总风险敞口"""
        total = 0.0
        for sym, pos in self._positions.items():
            price = price_map.get(sym, 0)
            if price > 0:
                total += pos.exposure(price, leverage)
        return total

    def register_callback(self, callback: Callable):
        """★ 新增：注册持仓变更回调"""
        self._callbacks.append(callback)

    def reset(self, symbol: str = None):
        if symbol:
            self._positions.pop(symbol, None)
            logger.info(f"[PositionManager] 重置: {symbol}")
        else:
            self._positions.clear()
            logger.info("[PositionManager] 全部重置")

    def snapshot(self) -> dict:
        """★ 新增：全量快照"""
        return {s: p.to_dict() for s, p in self._positions.items()}

    def restore(self, snapshot: dict):
        """★ 新增：从快照恢复"""
        for sym, data in snapshot.items():
            self._positions[sym] = Position.from_dict(data)
        logger.info(f"[PositionManager] 恢复 {len(snapshot)} 个持仓")
