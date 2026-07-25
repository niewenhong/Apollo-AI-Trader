"""
ai/stock_selector.py - Apollo-AI-Trader v2.7.0
AI选股：富途行情 → 多维指标评分 → 排序 → 写数据库 → 供策略自动读取
修复：save_stock_pool→add_to_pool，vt_symbol→stock_code，参数格式统一为字典列表
"""
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import numpy as np

try:
    from futu import RET_OK, KLType
except ImportError:
    RET_OK = 0
    KLType = None


class AIStockSelector:
    """技术面+资金面AI选股器"""

    def __init__(self, quote_ctx, db, top_n: int = 30, market: str = "US"):
        self.ctx = quote_ctx
        self.db = db
        self.top_n = top_n
        self.market = market
        self.min_score = 55.0

    def select(self, universe: Optional[List[str]] = None) -> List[Dict]:
        if universe is None:
            universe = self._get_default_universe()
        scored = []
        for code in universe:
            try:
                s = self._score(code)
                if s["score"] >= self.min_score:
                    scored.append(s)
            except Exception as e:
                print(f"[Selector] {code} 失败: {e}")
            time.sleep(0.3)
        scored.sort(key=lambda x: x["score"], reverse=True)
        selected = scored[:self.top_n]
        # 写入数据库
        pool = []
        for s in selected:
            stock_code = s["vt_symbol"]
            pool.append({
                "stock_code": stock_code,
                "market": self.market,
                "score": s["score"],
                "reason": s.get("reason", ""),
                "indicators": s.get("indicators", {}),
                "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
                "status": "selected"
            })
            # 同时加入执行池（使用字典列表格式调用 add_to_pool）
            self.db.add_to_pool([{
                "stock_code": stock_code,
                "market": self.market,
                "strategy_class": "MultiIndicatorStrategy"
            }])
        # 批量写入选股池
        self.db.add_to_pool(pool)
        print(f"[Selector] ✅ {len(selected)} 只写入 ai_stock_pool")
        return selected

    def _score(self, code: str) -> Dict:
        indicators = {}
        # 快照（修复：使用 *_ 吸收多余返回值）
        snap_result = self.ctx.get_market_snapshot([code])
        ret, data, *_ = snap_result
        if ret != RET_OK or data.empty:
            raise RuntimeError("snapshot fail")
        row = data.iloc[0]
        last = float(row.get("last_price", 0))
        prev = float(row.get("prev_close_price", last))
        chg = (last - prev) / prev if prev else 0
        turnover = float(row.get("turnover", 0))

        # K线（修复：使用 *_ 吸收多余返回值）
        kline_result = self.ctx.request_history_kline(
            code, ktype=KLType.K_DAY if KLType else "K_DAY",
            start=(datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d"),
            end=datetime.now().strftime("%Y-%m-%d"), max_count=120)
        ret, k, *_ = kline_result
        if ret != RET_OK or k.empty:
            raise RuntimeError("kline fail")
        c = k["close"].astype(float).values
        v = k["volume"].astype(float).values

        ma5, ma10, ma20, ma60 = self._ma(c, 5), self._ma(c, 10), self._ma(c, 20), self._ma(c, 60)
        rsi = self._rsi(c, 14)
        macd, sig, hist = self._macd(c)
        vr = float(v[-1]) / (self._ma(v, 20) + 1e-6)

        indicators = {"last": last, "chg": chg, "ma5": ma5, "ma20": ma20, "rsi": rsi, "macd_hist": hist, "vr": vr}

        score = 50.0
        reasons = []
        if ma5 > ma10 > ma20:
            score += 15
            reasons.append("多头排列")
        if last > ma20:
            score += 10
            reasons.append("站上MA20")
        if 30 < rsi < 70:
            score += 10
            reasons.append(f"RSI{rsi:.0f}")
        if hist > 0:
            score += 10
            reasons.append("MACD金叉")
        if vr > 1.5:
            score += 5
            reasons.append(f"放量{vr:.1f}x")
        if 0.01 < chg < 0.05:
            score += 5
            reasons.append(f"温和涨{chg*100:.1f}%")
        if turnover > 1e8:
            score += 5
            reasons.append("大市值")
        score = min(score, 100.0)

        return {
            "vt_symbol": self._to_vt(code),
            "code": code,
            "score": round(score, 2),
            "reason": ";".join(reasons),
            "indicators": indicators
        }

    def _get_default_universe(self) -> List[str]:
        if self.market == "US":
            return ["US.NVDA", "US.AAPL", "US.MSFT", "US.AMZN", "US.TSLA",
                    "US.META", "US.GOOGL", "US.AMD", "US.NFLX", "US.BABA"]
        return ["HK.00700", "HK.09988", "HK.03690", "HK.00388", "HK.00941"]

    @staticmethod
    def _to_vt(code):
        return code.replace("US.", "") + ".SMART" if code.startswith("US.") else code.replace("HK.", "") + ".SEHK"

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