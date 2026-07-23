"""
strategies/options/base_option_strategy.py - Apollo-AI-Trader v2.6.0
期权策略基类：封装期权通用逻辑（查询链、筛选合约、展期、平仓）
"""
from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData, TickData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, OrderType
import time
from typing import Optional, Dict, List, Tuple


class BaseOptionStrategy(CtaTemplate):
    """期权策略基类"""

    # 通用参数
    min_days_to_expiry = 7
    max_days_to_expiry = 45
    min_otm_prob = 0.60
    min_annual_roi = 0.30
    max_positions = 5
    position_size = 1  # 合约张数

    variables = ["net_premium", "max_loss", "max_profit", "pnl", "legs"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.net_premium = 0.0
        self.max_loss = 0.0
        self.max_profit = 0.0
        self.pnl = 0.0
        self.legs: Dict[str, dict] = {}  # leg_name -> order info

    def on_init(self):
        self.write_log(f"[{self.__class__.__name__}] on_init | {self.vt_symbol}")

    def on_start(self):
        self.write_log(f"[{self.__class__.__name__}] on_start | {self.vt_symbol}")

    def on_stop(self):
        self.write_log(f"[{self.__class__.__name__}] on_stop | {self.vt_symbol}")

    def on_tick(self, tick: TickData):
        pass  # 子类可重写

    def on_bar(self, bar: BarData):
        pass  # 子类实现

    # ── 通用工具 ──────────────────────────────────
    def _query_option_chain(self, code: str, expiry_start: str = "",
                            expiry_end: str = "") -> list:
        """查询期权链（通过FutuGateway）"""
        gw = self.cta_engine.main_engine.get_gateway("FUTU_US") or \
             self.cta_engine.main_engine.get_gateway("FUTU_HK")
        if not gw or not hasattr(gw, "quote_ctx"):
            return []
        try:
            from futu import OPTION_TYPE, OPTION_FIELD
            ret, data = gw.quote_ctx.get_option_expiration_date(code)
            if ret != 0: return []
            # 简化：返回可用到期日列表
            return data.to_dict("records") if hasattr(data,"to_dict") else []
        except Exception as e:
            self.write_log(f"[Option] 查询期权链失败: {e}")
            return []

    def _select_contracts(self, chain: list, leg_type: str) -> list:
        """从期权链中筛选符合条件的合约"""
        selected = []
        for item in chain:
            days = item.get("days_to_expiry", 999)
            if not (self.min_days_to_expiry <= days <= self.max_days_to_expiry):
                continue
            otm = item.get("otm_prob", 0)
            if otm < self.min_otm_prob: continue
            roi = item.get("annual_roi", 0)
            if roi < self.min_annual_roi: continue
            if leg_type == "put" and not item.get("is_put", True): continue
            if leg_type == "call" and not item.get("is_call", True): continue
            selected.append(item)
        return selected

    def _open_spread(self, long_leg: dict, short_leg: dict) -> bool:
        """开仓价差组合"""
        long_ok = self._send_option_order(long_leg, Direction.LONG, Offset.OPEN)
        short_ok = self._send_option_order(short_leg, Direction.SHORT, Offset.OPEN)
        if long_ok and short_ok:
            self.net_premium = long_leg.get("premium",0) - short_leg.get("premium",0)
            return True
        return False

    def _send_option_order(self, leg: dict, direction: Direction,
                           offset: Offset) -> bool:
        """发送期权下单请求"""
        try:
            from vnpy.trader.object import OrderRequest
            req = OrderRequest(
                symbol=leg["code"], exchange=leg.get("exchange","SMART"),
                direction=direction, type=OrderType.LIMIT,
                volume=leg.get("size", self.position_size),
                price=leg.get("limit_price", leg.get("mid_price",0)),
                offset=offset, reference=f"option_{self.strategy_name}")
            gw_name = "FUTU_US" if ".US." in self.vt_symbol else "FUTU_HK"
            vt_oid = self.cta_engine.main_engine.send_order(req, gw_name)
            leg["vt_orderid"] = vt_oid
            self.legs[leg.get("name","leg")] = leg
            return bool(vt_oid)
        except Exception as e:
            self.write_log(f"[Option] 下单失败: {e}")
            return False

    def _close_all_legs(self):
        """平掉所有腿"""
        for name, leg in list(self.legs.items()):
            direction = Direction.SHORT if leg.get("is_long") else Direction.LONG
            offset = Offset.CLOSE
            self._send_option_order(leg, direction, offset)
        self.legs.clear()

    def _roll_positions(self):
        """展期：平仓近月，开仓远月"""
        self._close_all_legs()
        # 子类实现重新开仓逻辑

    def _manage_expiry(self, bar: BarData):
        """管理到期：临近到期3天内平仓"""
        for name, leg in self.legs.items():
            days = leg.get("days_to_expiry", 999)
            if days <= 3:
                self.write_log(f"[Option] {name} 临近到期({days}天)，平仓")
                self._close_all_legs()
                return True
        return False
