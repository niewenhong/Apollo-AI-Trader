"""
multi_period_kline_handler.py — 多周期K线回调 v2.7.0
功能：接收富途原生K_1M/K_5M/K_15M/K_60M推送，转换为BarData并分发
版本：v2.7.0
变更：2026-07-26 适配vnpy 4.4.0 Event路径，去除try/except
"""

from futu import CurKlineHandlerBase, KLType, RET_OK
from vnpy.trader.object import BarData, Exchange, Interval
from vnpy.event import Event
from datetime import datetime
import logging

EVENT_BAR = "eBar"
logger = logging.getLogger(__name__)


class MultiPeriodKlineHandler(CurKlineHandlerBase):
    INTERVAL_MAP = {
        KLType.K_1M:  (Interval.MINUTE, 1),
        KLType.K_5M:  (Interval.MINUTE, 5),
        KLType.K_15M: (Interval.MINUTE, 15),
        KLType.K_60M: (Interval.HOUR,   1),
    }

    def __init__(self, gateway, market_bus=None):
        super().__init__()
        self.gateway = gateway
        self.market_bus = market_bus

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != RET_OK:
            return ret, data

        ktype = data.get("ktype")
        info = self.INTERVAL_MAP.get(ktype)
        if not info:
            return ret, data

        interval, window = info
        code = data["code"]
        exch = Exchange.SEHK if code.startswith("HK.") else Exchange.SMART

        bar = BarData(
            symbol=code, exchange=exch,
            interval=interval, window=window,
            datetime=datetime.strptime(data["time_key"], "%Y-%m-%d %H:%M:%S"),
            open_price=float(data["open"]),
            high_price=float(data["high"]),
            low_price=float(data["low"]),
            close_price=float(data["close"]),
            volume=float(data["volume"]),
            turnover=float(data.get("turnover", 0)),
            gateway_name=self.gateway.gateway_name,
        )

        if self.market_bus and hasattr(self.market_bus, 'db') and self.market_bus.db:
            try:
                self.market_bus.db.save_bar(bar)
            except Exception as e:
                self.gateway.write_log(f"[KlineHandler] 落库失败: {e}")

        self.gateway.event_engine.put(Event(EVENT_BAR, bar))
        self.gateway.write_log(
            f"[BAR] {code} {interval}{window} "
            f"O={bar.open_price} H={bar.high_price} "
            f"L={bar.low_price} C={bar.close_price} V={bar.volume}"
        )
        return ret, data