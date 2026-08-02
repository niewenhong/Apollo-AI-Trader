"""
ai/stock_selector.py — AI选股器 v3.0.2
==========================================
v3.0.2 - 接入修复版 KlineProvider（空缓存不再污染，支持重试）
v3.0.1 - 修复 add_to_pool 调用方式（逐条插入）
v3.0.0 - 日K 由 Provider 统一拉取+缓存
功能：技术面+资金面评分，排序后写入 ai_stock_pool。
"""

import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import numpy as np

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

from core.kline_provider import KlineProvider


class AIStockSelector:
    def __init__(self, quote_ctx, db=None, top_n: int = 30,
                 market: str = "US",
                 kline_provider: Optional[KlineProvider] = None):
        self.ctx = quote_ctx
        self.db = db
        self.top_n = top_n
        self.market = market
        self.min_score = 55.0
        self.kp = kline_provider

    def select(self, universe: Optional[List[str]] = None) -> List[Dict]:
        if universe is None:
            universe = self._get_default_universe()

        # 预热：把本批股票的日K一次性拉满缓存
        # universe 中的格式为 'US.NVDA'，KlineProvider 会自动转为 'NVDA.SMART' 作为缓存 key
        if self.kp is not None:
            self.kp.preload(universe, ktype="K_DAY", days=120)

        scored = []
        for code in universe:
            try:
                s = self._score(code)
                if s["score"] >= self.min_score:
                    scored.append(s)
            except Exception as e:
                print(f"[Selector] {code} 评分失败: {e}")
            time.sleep(0.3)

        scored.sort(key=lambda x: x["score"], reverse=True)
        selected = scored[:self.top_n]

        if self.db:
            for s in selected:
                vt = s["vt_symbol"]
                try:
                    self.db.add_to_pool(
                        symbol=vt,
                        score=s["score"],
                        reason=s.get("reason", ""),
                        market=self.market,
                        source="selector"
                    )
                except Exception as e:
                    print(f"[Selector] add_to_pool 失败 {vt}: {e}")
            print(f"[Selector] ✅ {len(selected)} 只写入 ai_stock_pool")

        return selected

    def _score(self, code: str) -> Dict:
        ret, data = self.ctx.get_market_snapshot([code])
        if ret != RET_OK or data.empty:
            raise RuntimeError(f"snapshot fail for {code}")

        row = data.iloc[0]
        last = float(row.get("last_price", 0))
        prev = float(row.get("prev_close_price", last))
        chg = (last - prev) / prev if prev else 0.0
        turnover = float(row.get("turnover", 0))

        vt = KlineProvider.futu_to_vt(code)
        if self.kp is not None:
            k = self.kp.get_daily(vt, days=120)
        else:
            from futu import KLType
            r2, k, *_ = self.ctx.request_history_kline(
                code, ktype=KLType.K_DAY,
                start=(datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d"),
                end=datetime.now().strftime("%Y-%m-%d"), max_count=120)
            if r2 != RET_OK:
                k = None

        if k is None or k.empty:
            raise RuntimeError(f"kline fail for {code}")

        c = k["close"].astype(float).values
        v = k["volume"].astype(float).values

        ma5 = self._ma(c, 5)
        ma10 = self._ma(c, 10)
        ma20 = self._ma(c, 20)
        rsi = self._rsi(c, 14)
        macd, sig, hist = self._macd(c)
        vr = float(v[-1]) / (self._ma(v, 20) + 1e-6)

        indicators = {
            "last": round(last, 4),
            "chg": round(chg, 6),
            "ma5": round(ma5, 4),
            "ma20": round(ma20, 4),
            "rsi": round(rsi, 2),
            "macd_hist": round(hist, 6),
            "vr": round(vr, 3),
        }

        score = 50.0
        reasons = []
        if ma5 > ma10 > ma20:
            score += 15; reasons.append("多头排列")
        if last > ma20:
            score += 10; reasons.append("站上MA20")
        if 30 < rsi < 70:
            score += 10; reasons.append(f"RSI{rsi:.0f}")
        if hist > 0:
            score += 10; reasons.append("MACD金叉")
        if vr > 1.5:
            score += 5; reasons.append(f"放量{vr:.1f}x")
        if 0.01 < chg < 0.05:
            score += 5; reasons.append(f"温和涨{chg*100:.1f}%")
        if turnover > 1e8:
            score += 5; reasons.append("大市值")
        score = min(score, 100.0)

        return {
            "vt_symbol": vt,
            "code": code,
            "score": round(score, 2),
            "reason": ";".join(reasons),
            "indicators": indicators,
        }

    def _get_default_universe(self) -> List[str]:
        if self.market == "US":
            return ["US.NVDA", "US.AAPL", "US.MSFT", "US.AMZN", "US.TSLA",
                    "US.META", "US.GOOGL", "US.AMD", "US.NFLX", "US.BABA"]
        return ["HK.00700", "HK.09988", "HK.03690", "HK.00388", "HK.00941"]

    @staticmethod
    def _ma(d, n):
        return float(np.mean(d[-n:])) if len(d) >= n else float(d[-1])

    @staticmethod
    def _rsi(c, n=14):
        if len(c) < n + 1:
            return 50.0
        d = np.diff(c[-(n + 1):])
        g = np.maximum(d, 0).sum() / n
        l = np.maximum(-d, 0).sum() / n
        return 100.0 if l == 0 else float(100 - 100 / (1 + g / (l + 1e-6)))

    @staticmethod
    def _macd(c, fast=12, slow=26, sig=9):
        if len(c) < slow + sig:
            return 0.0, 0.0, 0.0
        ema_f = AIStockSelector._ema(c, fast)
        ema_s = AIStockSelector._ema(c, slow)
        m = ema_f - ema_s
        s = AIStockSelector._ema(np.r_[np.array([m[:slow].mean()]), m[slow:]], sig)
        return float(m[-1]), float(s[-1]), float(m[-1] - s[-1])

    @staticmethod
    def _ema(d, n):
        r = np.zeros_like(d)
        r[0] = d[0]
        k = 2.0 / (n + 1)
        for i in range(1, len(d)):
            r[i] = d[i] * k + r[i - 1] * (1 - k)
        return r