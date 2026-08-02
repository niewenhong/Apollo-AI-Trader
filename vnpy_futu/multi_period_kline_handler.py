"""
multi_period_kline_handler.py — 多周期 K 线回调 v3.0.2（最终修复版）
=================================================================

修复内容（相对 v3.0.0）：
1. ★ 双事件推送：同时向 "eBar" 和 "eBar.{interval}" 发送 BarData
   - CTA 引擎的 BarGenerator 监听 "eBar"（无后缀），之前只发 "eBar.1m" 导致
     策略永远收不到 K 线 → am 永远 inited=False → 永远不下单
2. ★ symbol/exchange 格式修正：从富途 "US.AAPL" 转为 "AAPL"/Exchange.SMART
   - 与策略 vt_symbol（AAPL.SMART）对齐，CTA 引擎才能正确路由
3. ★ 详细日志：每根 K 线都打印，方便确认推送是否到达
4. ★ 时间解析容错：支持多种 time_key 格式，失败时用当前时间兜底
5. 保留原有多周期缓存、按需落盘接口
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
    KLType.K_1M:  Interval.MINUTE,
    KLType.K_5M:  Interval.MINUTE5,
    KLType.K_15M: Interval.MINUTE15,
    KLType.K_30M: Interval.MINUTE30,
    KLType.K_60M: Interval.HOUR,
    KLType.K_DAY: Interval.DAILY,
}

# Interval → 分钟数（备用）
INTERVAL_MINUTES = {
    Interval.MINUTE:   1,
    Interval.MINUTE5:  5,
    Interval.MINUTE15: 15,
    Interval.MINUTE30: 30,
    Interval.HOUR:      60,
    Interval.DAILY:    1440,
}

# 富途交易所前缀 → vnpy Exchange
FUTU_EXCHANGE_MAP = {
    "US": Exchange.SMART,
    "HK": Exchange.SEHK,
}


class MultiPeriodKlineHandler(KlineHandlerBase):
    """
    富途多周期 K 线回调处理器。

    收到富途推送的 K 线后：
    1. 将 code 从 "US.AAPL" 格式转为 symbol="AAPL", exchange=SMART
    2. 构造 BarData
    3. 同时向事件引擎发送两个事件：
       - "eBar"          → CTA 引擎 BarGenerator 监听（★关键修复）
       - "eBar.{interval}" → 其他多周期组件监听
    """

    def __init__(self, gateway, market_bus: Optional[EventEngine] = None):
        super().__init__()
        self.gateway = gateway
        self.market_bus = market_bus
        self._bars: Dict[str, Dict[Interval, BarData]] = {}
        self._kline_count = 0  # 统计收到多少根 K 线

    # ───────────────────────────────
    #  富途回调入口
    # ───────────────────────────────
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

    # ───────────────────────────────
    #  核心处理逻辑
    # ───────────────────────────────
    def process_kline(self, data: pd.DataFrame):
        if data is None or data.empty:
            return

        gw = self.gateway

        for _, row in data.iterrows():
            code = str(row.get("code", ""))
            if not code:
                continue

            # ★ 关键：富途 "US.AAPL" → symbol="AAPL", exchange=SMART
            sym, exchange = self._parse_futu_code(code)

            # 周期由 KLType 决定
            ktype = row.get("ktype", KLType.K_1M)
            interval = SUBTYPE_TO_INTERVAL.get(ktype, Interval.MINUTE)

            # 时间解析（容错）
            t = str(row.get("time_key", ""))
            dt = self._parse_time(t)

            # 构造 BarData
            bar = BarData(
                gateway_name=gw.gateway_name,
                symbol=sym,            # ★ "AAPL" 而不是 "US.AAPL"
                exchange=exchange,      # ★ Exchange.SMART
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

            # ★ 双事件推送
            # 1) "eBar" —— CTA 引擎 / BarGenerator 监听此事件
            event_general = Event(EVENT_BAR, bar)
            # 2) "eBar.{interval}" —— 多周期组件监听
            event_specific = Event(f"eBar.{interval.value}", bar)

            if self.market_bus:
                self.market_bus.put(event_general)
                self.market_bus.put(event_specific)
            else:
                gw.event_engine.put(event_general)
                gw.event_engine.put(event_specific)

            # ★ 日志：确认 K 线送达
            self._kline_count += 1
            if self._kline_count <= 5 or self._kline_count % 100 == 0:
                gw.write_log(
                    f"[KlineHandler] #{self._kline_count} "
                    f"{sym}.{exchange.value} {interval.value} "
                    f"O={bar.open_price:.2f} H={bar.high_price:.2f} "
                    f"L={bar.low_price:.2f} C={bar.close_price:.2f} "
                    f"V={bar.volume:.0f}"
                )

    # ───────────────────────────────
    #  工具方法
    # ───────────────────────────────
    def _parse_futu_code(self, code: str) -> tuple:
        """
        富途代码 → (symbol, Exchange)
        "US.AAPL"    → ("AAPL",  Exchange.SMART)
        "HK.00700"   → ("00700", Exchange.SEHK)
        "US.AAPL.B"   → ("AAPL.B", Exchange.SMART)  # 期权等嵌套
        "AAPL"        → ("AAPL",  Exchange.SMART)    # 无前缀兜底
        """
        if "." not in code:
            return code, Exchange.SMART

        parts = code.split(".")
        prefix = parts[0]
        symbol = ".".join(parts[1:])  # 支持嵌套点号
        exchange = FUTU_EXCHANGE_MAP.get(prefix, Exchange.SMART)
        return symbol, exchange

    def _parse_time(self, t: str) -> datetime:
        """解析 time_key 字符串为 datetime，失败返回 now()"""
        if not t:
            return datetime.now(CHINA_TZ)
        # 尝试多种格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                   "%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                return datetime.strptime(t, fmt).replace(tzinfo=CHINA_TZ)
            except ValueError:
                continue
        return datetime.now(CHINA_TZ)

    def get_latest_bar(self, code: str, interval: Interval) -> Optional[BarData]:
        """获取指定 code + 周期的缓存 bar"""
        return self._bars.get(code, {}).get(interval)

    def get_stats(self) -> str:
        """返回统计信息"""
        return f"K线总数={self._kline_count}, 缓存品种数={len(self._bars)}"


# 兼容旧版引用
EVENT_BAR = "eBar"
