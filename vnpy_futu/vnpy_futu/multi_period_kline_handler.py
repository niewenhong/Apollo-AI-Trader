"""
multi_period_kline_handler.py — 富途原生多周期K线回调 v2.7.0
- 接收 K_1M/K_5M/K_15M/K_60M 推送
- 转换为 vn.py BarData 事件分发
- 同时通过 MarketDataBus 落库
"""

from futu import CurKlineHandlerBase, KLType, RET_OK
from vnpy.trader.object import BarData, Exchange, Interval
from vnpy.trader.event import Event, EVENT_BAR
from datetime import datetime
import logging

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

        try:
            dt = datetime.strptime(data["time_key"], "%Y-%m-%d %H:%M:%S")
        except:
            dt = datetime.now()

        bar = BarData(
            symbol=code, exchange=exch,
            interval=interval, window=window,
            datetime=dt,
            open_price=float(data.get("open", 0)),
            high_price=float(data.get("high", 0)),
            low_price=float(data.get("low", 0)),
            close_price=float(data.get("close", 0)),
            volume=float(data.get("volume", 0)),
            turnover=float(data.get("turnover", 0)),
            gateway_name=self.gateway.gateway_name,
        )

        # 直接落库
        if self.market_bus:
            try:
                self.market_bus.db.save_bar(bar)
            except Exception as e:
                logger.error(f"K线落库失败: {e}")

        # 事件分发
        self.gateway.event_engine.put(Event(EVENT_BAR, bar))
        logger.debug(f"[BAR] {code} {interval}{window} "
                     f"O={bar.open_price} H={bar.high_price} "
                     f"L={bar.low_price} C={bar.close_price} V={bar.volume}")
        return ret, data
