# -*- coding: utf-8 -*-
"""持仓管理（多账户汇总、逐日盯市）"""
import logging
from typing import Dict, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("core.position_manager")

@dataclass
class Position:
    """单标的持仓"""
    symbol: str
    exchange: str
    direction: str  # "long" / "short"
    volume: int = 0
    avg_price: float = 0.0
    last_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return self.volume * self.last_price

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

class PositionManager:
    """多账户持仓管理"""

    def __init__(self):
        # account -> symbol -> Position
        self._positions: Dict[str, Dict[str, Position]] = {}
        self._lock = __import__("threading").Lock()

    def update_position(self, account: str, pos: Position):
        """更新持仓"""
        with self._lock:
            if account not in self._positions:
                self._positions[account] = {}
            self._positions[account][pos.symbol] = pos

    def get_position(self, account: str, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(account, {}).get(symbol)

    def get_all_positions(self, account: str = None) -> Dict[str, Position]:
        with self._lock:
            if account:
                return dict(self._positions.get(account, {}))
            # 汇总所有账户
            merged: Dict[str, Position] = {}
            for acc, pos_dict in self._positions.items():
                for sym, p in pos_dict.items():
                    if sym not in merged:
                        merged[sym] = Position(symbol=sym, exchange=p.exchange, direction="long")
                    merged[sym].volume += p.volume
                    merged[sym].realized_pnl += p.realized_pnl
                    merged[sym].unrealized_pnl += p.unrealized_pnl
            return merged

    def update_price(self, symbol: str, price: float):
        """更新最新价（逐日盯市）"""
        with self._lock:
            for acc, pos_dict in self._positions.items():
                if symbol in pos_dict:
                    p = pos_dict[symbol]
                    p.last_price = price
                    if p.volume > 0:
                        p.unrealized_pnl = (price - p.avg_price) * p.volume
                    elif p.volume < 0:
                        p.unrealized_pnl = (p.avg_price - price) * abs(p.volume)

    def get_total_equity(self, account: str, cash: float = 0.0) -> float:
        """计算账户总权益"""
        positions = self.get_all_positions(account)
        total_unrealized = sum(p.unrealized_pnl for p in positions.values())
        return cash + total_unrealized

    def get_total_pnl(self, account: str = None) -> float:
        positions = self.get_all_positions(account)
        return sum(p.total_pnl for p in positions.values())

    def clear_account(self, account: str):
        with self._lock:
            self._positions.pop(account, None)
