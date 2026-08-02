"""
core/position_manager.py - 智能持仓管理
提供持仓查询、更新、风险敞口计算
"""
from typing import Dict, Optional
import logging

logger = logging.getLogger("PositionManager")

class Position:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.long_qty = 0
        self.short_qty = 0
        self.avg_long_price = 0.0
        self.avg_short_price = 0.0
        self.realized_pnl = 0.0

    @property
    def net_qty(self) -> int:
        return self.long_qty - self.short_qty

    @property
    def gross_qty(self) -> int:
        return self.long_qty + self.short_qty

    def update(self, direction: str, qty: int, price: float):
        """外部调用，更新持仓"""
        if direction == 'LONG':
            self._add_long(qty, price)
        elif direction == 'SHORT':
            self._add_short(qty, price)
        else:
            logger.warning(f"未知方向: {direction}")

    def _add_long(self, qty: int, price: float):
        if self.short_qty > 0:
            # 先平空
            close_qty = min(qty, self.short_qty)
            self.realized_pnl += (self.avg_short_price - price) * close_qty
            self.short_qty -= close_qty
            qty -= close_qty
            if self.short_qty == 0:
                self.avg_short_price = 0.0
        if qty > 0:
            total = self.avg_long_price * self.long_qty + price * qty
            self.long_qty += qty
            self.avg_long_price = total / self.long_qty

    def _add_short(self, qty: int, price: float):
        if self.long_qty > 0:
            close_qty = min(qty, self.long_qty)
            self.realized_pnl += (price - self.avg_long_price) * close_qty
            self.long_qty -= close_qty
            qty -= close_qty
            if self.long_qty == 0:
                self.avg_long_price = 0.0
        if qty > 0:
            total = self.avg_short_price * self.short_qty + price * qty
            self.short_qty += qty
            self.avg_short_price = total / self.short_qty

    def unrealized_pnl(self, current_price: float) -> float:
        pnl = 0.0
        if self.long_qty > 0:
            pnl += (current_price - self.avg_long_price) * self.long_qty
        if self.short_qty > 0:
            pnl += (self.avg_short_price - current_price) * self.short_qty
        return pnl

    def total_pnl(self, current_price: float) -> float:
        return self.realized_pnl + self.unrealized_pnl(current_price)

    def exposure(self, current_price: float) -> float:
        """风险敞口（绝对值）"""
        return abs(self.net_qty) * current_price

    def __repr__(self):
        return (f"Position({self.symbol}: L={self.long_qty}@{self.avg_long_price:.2f}, "
                f"S={self.short_qty}@{self.avg_short_price:.2f}, "
                f"net={self.net_qty}, rPNL={self.realized_pnl:.2f})")


class PositionManager:
    def __init__(self):
        self._positions: Dict[str, Position] = {}

    def get_or_create(self, symbol: str) -> Position:
        if symbol not in self._positions:
            self._positions[symbol] = Position(symbol)
        return self._positions[symbol]

    def update(self, symbol: str, direction: str, qty: int, price: float):
        pos = self.get_or_create(symbol)
        pos.update(direction, qty, price)
        logger.info(f"[PositionManager] {symbol} 更新: {pos}")

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_net_qty(self, symbol: str) -> int:
        pos = self._positions.get(symbol)
        return pos.net_qty if pos else 0

    def get_all_positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    def reset(self, symbol: str = None):
        if symbol:
            self._positions.pop(symbol, None)
        else:
            self._positions.clear()