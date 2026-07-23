"""
futu_data_enricher.py — Apollo-AI-Tra-der v2.4.0
富途数据增强器（6 维数据：价格/成交量/买卖盘/价差/换手率/52周高低）
"""
import time
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime

import pandas as pd
from futu import OpenQuoteContext, RET_OK


class FutuDataEnricher:
    """从富途行情接口获取增强数据，为 AI 选股和策略提供 6 维特征"""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, password: str = ""):
        self.host = host
        self.port = port
        self.password = password
        self.ctx: Optional[OpenQuoteContext] = None
        self.mode = "mock"
        self._connected = False
        self._cache: Dict[str, dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ========== 连接 ==========
    def start(self):
        try:
            self.ctx = OpenQuoteContext(self.host, self.port)
            ret, data = self.ctx.get_global_state()
            if ret == RET_OK:
                self.mode = "real"
                self._connected = True
                print(f"[FutuDataEnricher] 行情上下文已连接 ({self.host}:{self.port})")
                print(f"[FutuDataEnricher] 模式: {self.mode} (已连接富途)")
            else:
                self.mode = "mock"
                print(f"[FutuDataEnricher] 连接失败，降级为 mock 模式: {data}")
        except Exception as e:
            self.mode = "mock"
            print(f"[FutuDataEnricher] 异常，降级为 mock: {e}")

        self._running = True
        self._thread = threading.Thread(target=self._background_refresh, daemon=True)
        self._thread.start()
        print(f"[FutuDataEnricher] 后台刷新已启动")

    def close(self):
        self._running = False
        if self.ctx:
            self.ctx.close()
            self.ctx = None
        print(f"[FutuDataEnricher] 已关闭")

    # ========== 后台刷新 ==========
    def _background_refresh(self):
        while self._running:
            try:
                with self._lock:
                    for symbol in list(self._cache.keys()):
                        self._refresh_symbol(symbol)
                time.sleep(60)  # 每分钟刷新一次
            except Exception as e:
                print(f"[FutuDataEnricher] 后台刷新异常: {e}")
                time.sleep(30)

    def _refresh_symbol(self, futu_symbol: str):
        if not self.ctx:
            return
        ret, data = self.ctx.get_market_snapshot([futu_symbol])
        if ret == RET_OK and len(data) > 0:
            row = data.iloc[0]
            self._cache[futu_symbol] = {
                "last_price": float(row.get("last_price", 0)),
                "open_price": float(row.get("open_price", 0)),
                "high_price": float(row.get("high_price", 0)),
                "low_price": float(row.get("low_price", 0)),
                "prev_close_price": float(row.get("prev_close_price", 0)),
                "volume": int(row.get("volume", 0)),
                "turnover": float(row.get("turnover", 0)),
                "bid_price": float(row.get("bid_price", 0)),
                "ask_price": float(row.get("ask_price", 0)),
                "bid_vol": int(row.get("bid_vol", 0)),
                "ask_vol": int(row.get("ask_vol", 0)),
                "amplitude": float(row.get("amplitude", 0)),
                "turnover_rate": float(row.get("turnover_rate", 0)),
                "highest52weeks": float(row.get("highest52weeks_price", 0)),
                "lowest52weeks": float(row.get("lowest52weeks_price", 0)),
                "updated": time.time(),
            }
            self._cache_time[futu_symbol] = time.time()

    # ========== 外部接口 ==========
    def get_enriched_data(self, symbol: str, market: str = "US") -> dict:
        """获取单只股票的 6 维增强数据"""
        futu_symbol = f"{market}.{symbol}"
        with self._lock:
            if futu_symbol not in self._cache:
                self._refresh_symbol(futu_symbol)
            return self._cache.get(futu_symbol, {}).copy()

    def enrich_pool_with_data(self, pool: List[dict]) -> List[dict]:
        """为选股池每只股票附加增强数据"""
        enriched = []
        for item in pool:
            if isinstance(item, dict):
                symbol = item.get("symbol", "").split('.')[0]
                market = item.get("market", "US")
            else:
                symbol = str(item).split('.')[0]
                market = "US"

            data = self.get_enriched_data(symbol, market)
            new_item = dict(item) if isinstance(item, dict) else {"symbol": symbol}
            new_item["enriched_data"] = data
            new_item["last_price"] = data.get("last_price", 0)
            new_item["volume"] = data.get("volume", 0)
            new_item["turnover_rate"] = data.get("turnover_rate", 0)
            new_item["amplitude"] = data.get("amplitude", 0)
            new_item["bid_ask_spread"] = data.get("ask_price", 0) - data.get("bid_price", 0)
            new_item["week52_high"] = data.get("highest52weeks", 0)
            new_item["week52_low"] = data.get("lowest52weeks", 0)
            enriched.append(new_item)
        return enriched

    def get_market_breadth(self, market: str = "US") -> dict:
        """获取市场宽度指标（涨跌比等）"""
        # 简化实现：返回缓存中所有标的的涨跌统计
        with self._lock:
            up = down = flat = 0
            for sym, d in self._cache.items():
                if not d:
                    continue
                change = d.get("last_price", 0) - d.get("prev_close_price", 0)
                if change > 0:
                    up += 1
                elif change < 0:
                    down += 1
                else:
                    flat += 1
            total = up + down + flat
            return {
                "market": market,
                "advance": up,
                "decline": down,
                "unchanged": flat,
                "total": total,
                "advance_decline_ratio": up / max(down, 1),
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
