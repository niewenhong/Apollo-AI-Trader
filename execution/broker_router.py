"""
多接口路由（Futu + IB）
统一下单/撤单接口，策略层无需关心底层是哪家券商。

路由规则：
- 港股标的（SEHK/SMART.HK）→ Futu
- 美股标的（SMART/NASDAQ/NYSE）→ Futu 或 IB
- 期货标的（HKFE/CME）→ 根据配置选择
"""
from typing import Optional, Dict
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import OrderData, TradeData, TickData, BarData

# 路由映射：交易所后缀 → gateway_name
EXCHANGE_ROUTING = {
    "SEHK":   "FUTU",
    "SMART":  "FUTU",     # 美股默认走富途
    "NASDAQ": "FUTU",
    "NYSE":   "FUTU",
    "HKFE":   "FUTU",     # 恒指期货
    "CME":    "IB",        # 美股期货走 IB
    "ISLAND": "IB",
    "ARCA":   "IB",
}


class BrokerRouter:
    """
    多券商下单路由
    策略调用 router.send_order()，由 router 决定走 Futu 还是 IB
    """

    def __init__(self, main_engine=None):
        self._engine = main_engine
        self._gateways = {}  # gateway_name → gateway 实例
        self._default_gateway = "FUTU"

    def register_gateway(self, name: str, gateway):
        """注册网关实例"""
        self._gateways[name] = gateway

    def route_for(self, vt_symbol: str) -> str:
        """根据 vt_symbol 的交易所后缀返回 gateway_name"""
        if "." not in vt_symbol:
            return self._default_gateway
        exchange = vt_symbol.rsplit(".", 1)[1].upper()
        return EXCHANGE_ROUTING.get(exchange, self._default_gateway)

    def send_order(
        self,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: int,
        order_type: OrderType = OrderType.LIMIT,
    ) -> Optional[str]:
        """统一下单接口"""
        gateway = self.route_for(vt_symbol)
        if gateway not in self._gateways:
            print(f"[BrokerRouter] 网关 {gateway} 未注册")
            return None
        gw = self._gateways[gateway]
        # 调用 vnpy gateway 的 send_order
        return gw.send_order(
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            order_type=order_type,
        )

    def cancel_order(self, vt_symbol: str, orderid: str):
        """统一撤单"""
        gateway = self.route_for(vt_symbol)
        if gateway in self._gateways:
            self._gateways[gateway].cancel_order(orderid)

    def get_account(self, gateway: str = "FUTU") -> dict:
        """获取账户信息"""
        if gateway in self._gateways:
            return self._gateways[gateway].get_account()
        return {}

    def get_position(self, vt_symbol: str) -> dict:
        """获取持仓"""
        gateway = self.route_for(vt_symbol)
        if gateway in self._gateways:
            return self._gateways[gateway].get_position(vt_symbol)
        return {}
