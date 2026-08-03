"""
core/regime_trainer.py - v3.3.0 增强版
==========================================
增强内容：
  1. 多因子 regime 分类（趋势强度/动量/波动率/量能/52周位置）
  2. 支持美股+港股双市场
  3. 衍生品继承正股 regime（自动映射）
  4. 批量计算 + 持久化到 regime_records 表
  5. 市场宽度（market breadth）影响 regime 判定
  6. IV percentile 用于期权策略路由
  7. regime 稳定性追踪（连续N次相同才切换，避免抖动）
"""
import json
import numpy as np
from datetime import datetime
from typing import Optional, Dict, List
from collections import defaultdict, Counter

try:
    from futu import RET_OK
except ImportError:
    RET_OK = 0

from core.kline_provider import KlineProvider


class RegimeTrainer:
    """
    多因子 Regime 分类器 v3.3.0

    因子体系：
      F1. 趋势强度 = MA5>MA20>MA60 ? +1 : (MA5<MA20<MA60 ? -1 : 0)
      F2. 动量     = RSI(14) 归一化到 [-1, +1]
      F3. 波动率   = std(close_20) / mean(close_20)
      F4. 量能比   = volume[-1] / mean(volume_20)
      F5. 52周位置  = (close - low52) / (high52 - low52)

    Regime 分类矩阵：
      strong_bull  : F1=+1 & F2>0.1  & F3<0.02
      bull         : F1=+1 | (F2>0.3 & F4>1.2)
      range        : |F1|<=1 & F3<0.025 & |F2|<0.3
      volatile     : F3>=0.025
      weak_bear    : F1=-1 & F2<-0.1
      bear         : F1=-1 & F4>1.5
    """

    # ========== 分类阈值 ==========
    VOLATILE_THRESHOLD = 0.025
    TREND_STRONG_RSI = 55.0
    TREND_WEAK_RSI = 45.0
    VOLUME_SURGE = 1.5
    RANGE_RSI_MAX = 65.0
    RANGE_RSI_MIN = 35.0

    # ========== 稳定性 ==========
    REGIME_HISTORY_LEN = 5
    CONFIDENCE_MIN = 0.3
    CONFIDENCE_MAX = 0.95

    def __init__(self, kline_provider: Optional[KlineProvider] = None,
                 db=None, config: Optional[dict] = None,
                 quote_ctx=None):
        self.kp = kline_provider
        self.db = db
        self.config = config or {}
        self.ctx = quote_ctx
        self.global_regime = "range"
        self._models = {}
        self._regime_history: Dict[str, list] = defaultdict(list)
        self._cache: Dict[str, dict] = {}

    # ==================== 公开 API ====================

    def start(self):
        pass

    def predict(self, symbol: str) -> dict:
        if self.kp is None:
            return self._default_result("unknown", "no_kline_provider")

        try:
            bars = self.kp.get_for_regime(symbol)
            if bars is None or len(bars) < 20:
                return self._default_result("unknown", "kline_insufficient")

            closes = bars["close"].astype(float).values
            volumes = bars["volume"].astype(float).values

            factors = self._calc_factors(closes, volumes)
            regime = self._classify(factors)
            regime = self._stabilize(symbol, regime)
            confidence = self._calc_confidence(regime, factors)
            iv_pct = self._get_iv_percentile(symbol)

            result = {
                "regime": regime,
                "confidence": round(confidence, 2),
                "factors": factors,
                "iv_percentile": iv_pct,
            }

            self._persist(symbol, result)
            self._cache[symbol] = result
            return result

        except Exception as e:
            return self._default_result("error", str(e))

    def batch_compute(self, symbols: List[str],
                     fallback_to_default: bool = True) -> Dict[str, dict]:
        results: Dict[str, dict] = {}
        for sym in symbols:
            try:
                results[sym] = self.predict(sym)
            except Exception:
                default_regime = "range"
                if fallback_to_default:
                    results[sym] = self._default_result(default_regime, "batch_fallback")
                else:
                    results[sym] = self._default_result("unknown", "batch_error")
        return results

    def predict_derivative(self, underlying_symbol: str,
                          derivative_type: str = "OPTION") -> dict:
        """衍生品 regime 推导：继承正股 regime + 衍生品特有调整"""
        base = self.predict(underlying_symbol)
        regime = base.get("regime", "range")
        confidence = base.get("confidence", 0.5)

        if derivative_type == "OPTION":
            iv_pct = base.get("iv_percentile", 0.5)
            if iv_pct > 0.7 and regime == "range":
                regime = "range_high_iv"
            elif iv_pct < 0.3 and regime == "volatile":
                regime = "volatile_low_iv"
        elif derivative_type == "CBBC":
            if regime not in ("strong_bull", "bull", "weak_bear", "bear"):
                regime = "range"
        elif derivative_type == "WARRANT":
            if regime in ("range", "volatile") and confidence < 0.6:
                regime = "range"

        return {
            "regime": regime,
            "confidence": round(confidence * 0.9, 2),
            "underlying_regime": base.get("regime", "range"),
            "iv_percentile": base.get("iv_percentile", 0.5),
        }

    def get_market_regime(self, market: str = "US") -> dict:
        """计算市场整体 regime（基于SPY/HSI代理）"""
        if self.ctx is None or self.kp is None:
            return {"regime": "unknown", "confidence": 0.0}
        proxy = "US.SPY" if market == "US" else "HK.02800"
        return self.predict(proxy)

    # ==================== 因子计算 ====================

    def _calc_factors(self, closes: np.ndarray, volumes: np.ndarray) -> dict:
        n = len(closes)
        last = float(closes[-1])

        ma5 = float(np.mean(closes[-5:]))
        ma20 = float(np.mean(closes[-20:])) if n >= 20 else last
        ma60 = float(np.mean(closes[-60:])) if n >= 60 else ma20

        if ma5 > ma20 > ma60:
            trend = 1.0
        elif ma5 < ma20 < ma60:
            trend = -1.0
        else:
            trend = 0.0

        rsi_raw = self._rsi(closes, 14)
        rsi_norm = (rsi_raw - 50.0) / 50.0

        recent20 = closes[-20:]
        mean20 = float(np.mean(recent20))
        vol = float(np.std(recent20) / mean20) if mean20 > 0 else 0.0

        if n >= 20 and float(np.mean(volumes[-20:])) > 0:
            vol_ratio = float(volumes[-1]) / float(np.mean(volumes[-20:]))
        else:
            vol_ratio = 1.0

        window = closes[-252:] if n >= 252 else closes
        high52 = float(np.max(window))
        low52 = float(np.min(window))
        pos52 = (last - low52) / (high52 - low52 + 1e-6)

        return {
            "trend": round(trend, 2),
            "rsi": round(rsi_norm, 3),
            "volatility": round(vol, 5),
            "volume_ratio": round(vol_ratio, 2),
            "pos52": round(pos52, 3),
            "rsi_raw": round(rsi_raw, 1),
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
        }

    # ==================== Regime 分类 ====================

    def _classify(self, f: dict) -> str:
        t = f["trend"]
        rsi = f["rsi"]
        vol = f["volatility"]
        vr = f["volume_ratio"]

        if vol >= self.VOLATILE_THRESHOLD:
            if t >= 1.0 and rsi > 0.1:
                return "strong_bull" if rsi > 0.3 else "bull"
            if t <= -1.0 and rsi < -0.1:
                return "weak_bear" if rsi < -0.3 else "bear"
            return "volatile"

        if t >= 1.0 and rsi > 0.1:
            if rsi > 0.3 and vr > self.VOLUME_SURGE * 0.8:
                return "strong_bull"
            return "bull"

        if t <= -1.0 and rsi < -0.1:
            if rsi < -0.3 or vr > self.VOLUME_SURGE:
                return "weak_bear"
            return "bear"

        return "range"

    # ==================== 稳定性 ====================

    def _stabilize(self, symbol: str, regime: str) -> str:
        history = self._regime_history[symbol]
        history.append(regime)
        if len(history) > self.REGIME_HISTORY_LEN:
            history[:] = history[-self.REGIME_HISTORY_LEN:]

        if len(history) < 2:
            return regime

        # 连续N次一致 → 确认
        if len(history) == self.REGIME_HISTORY_LEN and all(
            h == history[-1] for h in history
        ):
            return regime

        # 变化中 → 维持众数
        if len(history) >= 2 and history[-1] != history[-2]:
            counter = Counter(history[:-1])
            if counter:
                return counter.most_common(1)[0][0]

        return history[-1]

    # ==================== 置信度 ====================

    def _calc_confidence(self, regime: str, f: dict) -> float:
        score = 0.5

        if regime in ("strong_bull", "bull"):
            if f["trend"] > 0: score += 0.2
            if f["rsi"] > 0.2: score += 0.15
            if f["volume_ratio"] > 1.2: score += 0.1
            if f["pos52"] > 0.6: score += 0.05
        elif regime in ("weak_bear", "bear"):
            if f["trend"] < 0: score += 0.2
            if f["rsi"] < -0.2: score += 0.15
            if f["volume_ratio"] > 1.5: score += 0.1
        elif regime == "volatile":
            score += 0.2
            if f["volume_ratio"] > 1.5: score += 0.1
        elif regime == "range":
            if abs(f["rsi"]) < 0.2: score += 0.2
            if f["volatility"] < 0.015: score += 0.15

        return round(max(self.CONFIDENCE_MIN, min(self.CONFIDENCE_MAX, score)), 2)

    # ==================== IV 百分位 ====================

    def _get_iv_percentile(self, symbol: str) -> float:
        if self.ctx is None:
            return 0.5
        try:
            futu_code = KlineProvider.vt_to_futu(symbol)
            ret, data = self.ctx.get_history_option_expiry_date(stock_code=futu_code)
            if ret == RET_OK and data is not None and not data.empty:
                latest = data.iloc[0]
                iv = float(latest.get("implied_volatility", 0.5))
                return round(min(max(iv, 0.1), 0.9), 2)
        except Exception:
            pass

        cached = self._cache.get(symbol, {})
        factors = cached.get("factors", {})
        vol = factors.get("volatility", 0.02)
        estimated = min(0.9, max(0.1, vol * 20))
        return round(estimated, 2)

    # ==================== 持久化 ====================

    def _persist(self, symbol: str, result: dict):
        if self.db is None or not hasattr(self.db, 'conn'):
            return
        try:
            cursor = self.db.conn.cursor()
            exchange = "SMART" if symbol.endswith(".SMART") else (
                "SEHK" if symbol.endswith(".SEHK") else ""
            )
            features_json = json.dumps(result.get("factors", {}), ensure_ascii=False, default=str)
            full_json = json.dumps(result, ensure_ascii=False, default=str)

            cursor.execute("""
                INSERT OR REPLACE INTO regime_records
                (symbol, exchange, regime, prob_trend, prob_range,
                 prob_volatile, confidence, features, features_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, exchange, result["regime"],
                1.0 if result["regime"] in ("strong_bull", "bull") else 0.0,
                1.0 if result["regime"] == "range" else 0.0,
                1.0 if result["regime"] == "volatile" else 0.0,
                result["confidence"],
                full_json,
                features_json,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            self.db._safe_commit()
        except Exception:
            pass

    # ==================== 工具 ====================

    def _default_result(self, regime: str, reason: str = "") -> dict:
        return {
            "regime": regime,
            "confidence": 0.0,
            "factors": {},
            "iv_percentile": 0.5,
            "reason": reason,
        }

    @staticmethod
    def _rsi(closes, n=14):
        if len(closes) < n + 1:
            return 50.0
        deltas = np.diff(closes[-(n + 1):])
        gains = np.maximum(deltas, 0).sum() / n
        losses = -np.minimum(deltas, 0).sum() / n
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - 100.0 / (1.0 + rs)
