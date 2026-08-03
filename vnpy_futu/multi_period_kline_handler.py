"""
multi_period_kline_handler.py — 多周期 K 线回调 v3.0.3（vnpy 4.4.0 正确修复版）
=================================================================

修复依据（查证源码）：
1. futu-api 官方实时K线回调基类 = CurKlineHandlerBase（非 KlineHandlerBase）
2. vnpy 4.4.0 Interval 枚举只有：MINUTE/HOUR/DAILY/WEEKLY/TICK
   不存在 MINUTE5/MINUTE15/MINUTE30，故 5/15/30/60 分钟统一映射到 HOUR
   真正的 N 分钟合成由 BarGenerator(window=N, interval=Interval.MINUTE) 完成
"""
from datetime import datetime
from typing import Dict, Optional
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval, Exchange
from vnpy.trader.object import BarData, TickData
from vnpy.trader.utility import ZoneInfo
import pandas as pd
from futu import KLType, CurKlineHandlerBase, RET_OK

CHINA_TZ = ZoneInfo("Asia/Shanghai")

# SubType → Interval 映射（严格遵循 vnpy 4.4.0 源码定义）
# vnpy Interval 枚举成员：MINUTE="1m", HOUR="1h", DAILY="d", WEEKLY="w", TICK="tick"
SUBTYPE_TO_INTERVAL = {
    KLType.K_1M:  Interval.MINUTE,   # "1m"
    KLType.K_5M:  Interval.HOUR,     # vnpy 无 MINUTE5，映射到 HOUR
    KLType.K_15M: Interval.HOUR,     # 同上
    KLType.K_30M: Interval.HOUR,     # 同上
    KLType.K_60M: Interval.HOUR,     # "1h"
    KLType.K_DAY: Interval.DAILY,    # "d"
}

# 富途交易所前缀 → vnpy Exchange
FUTU_EXCHANGE_MAP = {
    "US": Exchange.SMART,
    "HK": Exchange.SEHK,
}


class MultiPeriodKlineHandler(CurKlineHandlerBase):
    """
    富途多周期 K 线回调处理器。

    收到富途推送的 K 线后：
    1. 将 code 从 "US.AAPL" 格式转为 symbol="AAPL", exchange=SMART
    2. 构造 BarData（interval 取自 SUBTYPE_TO_INTERVAL）
    3. 同时向事件引擎发送两个事件：
       - "eBar"          → CTA 引擎 BarGenerator 监听
       - "eBar.{interval}" → 其他多周期组件监听
    """

    def __init__(self, gateway, market_bus: Optional[EventEngine] = None):
        super().__init__()
        self.gateway = gateway
        self.market_bus = market_bus
        self._bars: Dict[str, Dict[Interval, BarData]] = {}
        self._kline_count = 0

    def on_recv_rsp(self, rsp_str):
        ret_code, content = super().on_recv_rsp(rsp_str)
        if ret_code != RET_OK:
            return ret_code, content
        try:
            self.process_kline(content)
        except Exception as e:
            self.gateway.write_log(f"[KlineHandler] ❌ 处理异常: {e}")
            import traceback
            traceback.print_exc()
        return RET_OK, content

    def process_kline(self, data: pd.DataFrame):
        if data is None or data.empty:
            return

        gw = self.gateway

        for _, row in data.iterrows():
            code = str(row.get("code", ""))
            if not code:
                continue

            # 富途 "US.AAPL" → symbol="AAPL", exchange=SMART
            sym, exchange = self._parse_futu_code(code)

            # 周期由 KLType 决定 → 映射到 vnpy Interval
            ktype = row.get("ktype", KLType.K_1M)
            interval = SUBTYPE_TO_INTERVAL.get(ktype, Interval.MINUTE)

            # 时间解析
            t = str(row.get("time_key", ""))
            dt = self._parse_time(t)

            # 构造 BarData
            bar = BarData(
                gateway_name=gw.gateway_name,
                symbol=sym,
                exchange=exchange,
                datetime=dt,
                interval=interval,
                volume=float(row.get("volume", 0) or 0),
                turnover=float(row.get("turnover", 0) or 0),
                open_interest=0,
                open_price=float(row.get("open", 0) or 0),
                high_price=float(row.get("high", 0) or 0),
                low_price=float(row.get("low", 0) or 0),
                close_price=float(row.get("close", 0) or 0),
            )

            # 缓存最新一根
            self._bars.setdefault(code, {})[interval] = bar

            # 双事件推送
            event_general = Event(EVENT_BAR, bar)
            event_specific = Event(f"eBar.{interval.value}", bar)

            if self.market_bus:
                self.market_bus.put(event_general)
                self.market_bus.put(event_specific)
            else:
                gw.event_engine.put(event_general)
                gw.event_engine.put(event_specific)

            # 日志
            self._kline_count += 1
            if self._kline_count <= 5 or self._kline_count % 100 == 0:
                gw.write_log(
                    f"[KlineHandler] #{self._kline_count} "
                    f"{sym}.{exchange.value} {interval.value} "
                    f"O={bar.open_price:.2f} H={bar.high_price:.2f} "
                    f"L={bar.low_price:.2f} C={bar.close_price:.2f} "
                    f"V={bar.volume:.0f}"
                )

    def _parse_futu_code(self, code: str) -> tuple:
        if "." not in code:
            return code, Exchange.SMART

        parts = code.split(".")
        prefix = parts[0]
        symbol = ".".join(parts[1:])
        exchange = FUTU_EXCHANGE_MAP.get(prefix, Exchange.SMART)
        return symbol, exchange

    def _parse_time(self, t: str) -> datetime:
        if not t:
            return datetime.now(CHINA_TZ)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                   "%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(t, fmt).replace(tzinfo=CHINA_TZ)
            except ValueError:
                continue
        return datetime.now(CHINA_TZ)

    def get_latest_bar(self, code: str, interval: Interval) -> Optional[BarData]:
        return self._bars.get(code, {}).get(interval)

    def get_stats(self) -> str:
        return f"K线总数={self._kline_count}, 缓存品种数={len(self._bars)}"


# 兼容旧版引用
EVENT_BAR = "eBar"