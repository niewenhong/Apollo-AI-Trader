"""
multi_period_kline_handler.py — 多周期 K 线回调
- 接收 SubType.K_1M/5M/15M/60M 推送，合成并分发到策略
"""
from datetime import datetime
from typing import Dict, Optional
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval, Exchange
from vnpy.trader.object import BarData, TickData
from vnpy.trader.utility import ZoneInfo
import pandas as pd
from futu import KLType, KlineHandlerBase, RET_OK

CHINA_TZ = ZoneInfo("Asia/Shanghai")

# SubType → Interval 映射
SUBTYPE_TO_INTERVAL = {
    KLType.K_1M: Interval.MINUTE,
    KLType.K_5M: Interval.MINUTE5,
    KLType.K_15M: Interval.MINUTE15,
    KLType.K_30M: Interval.MINUTE30,
    KLType.K_60M: Interval.HOUR,
    KLType.K_DAY: Interval.DAILY,
}

# Interval 分钟数
INTERVAL_MINUTES = {
    Interval.MINUTE: 1,
    Interval.MINUTE5: 5,
    Interval.MINUTE15: 15,
    Interval.MINUTE30: 30,
    Interval.HOUR: 60,
    Interval.DAILY: 1440,
}


class MultiPeriodKlineHandler(KlineHandlerBase):
    """富途多周期 K 线回调处理器。"""

    def __init__(self, gateway, market_bus: Optional[EventEngine] = None):
        super().__init__()
        self.gateway = gateway
        self.market_bus = market_bus
        self._bars: Dict[str, Dict[Interval, BarData]] = {}

    def on_recv_rsp(self, rsp_str):
        ret_code, content = super().on_recv_rsp(rsp_str)
        if ret_code != RET_OK:
            return ret_code, content
        try:
            self.process_kline(content)
        except Exception as e:
            self.gateway.write_log(f"[KlineHandler] error: {e}")
        return RET_OK, content

    def process_kline(self, data: pd.DataFrame):
        if data is None or data.empty:
            return
        gw = self.gateway
        for _, row in data.iterrows():
            code = str(row.get("code", ""))
            if not code:
                continue
            sym, ex = gw.__class__._futu2vt(code) if hasattr(gw.__class__, '_futu2vt') else (code.split('.')[-1], Exchange.SMART)
            # 周期由 KLType 决定（在 data 里通常是固定的一列或外部已知）
            ktype = row.get("ktype", KLType.K_1M)
            interval = SUBTYPE_TO_INTERVAL.get(ktype, Interval.MINUTE)
            t = str(row.get("time_key", ""))
            try:
                dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TZ)
            except ValueError:
                dt = datetime.now(CHINA_TZ)

            bar = BarData(
                gateway_name=gw.gateway_name,
                symbol=sym, exchange=ex,
                datetime=dt, interval=interval,
                volume=float(row.get("volume", 0) or 0),
                turnover=float(row.get("turnover", 0) or 0),
                open_interest=0,
                open_price=float(row.get("open", 0) or 0),
                high_price=float(row.get("high", 0) or 0),
                low_price=float(row.get("low", 0) or 0),
                close_price=float(row.get("close", 0) or 0),
            )
            # 缓存最新一根
            d = self._bars.setdefault(code, {})
            d[interval] = bar
            # 分发
            evt_name = f"eBar.{interval.value}"
            if self.market_bus:
                self.market_bus.put(Event(evt_name, bar))
            else:
                gw.event_engine.put(Event(evt_name, bar))

    def get_latest_bar(self, code: str, interval: Interval) -> Optional[BarData]:
        return self._bars.get(code, {}).get(interval)
