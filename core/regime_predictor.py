# -*- coding: utf-8 -*-
"""
core/regime_predictor.py - 全自动自适应 Regime 预测器（v4.6.6 修复版）

变更记录：
  v4.6.6 - ★ 核心修复：_lazy_get_underlying 中 get_warrant 返回值解包
           - get_warrant 返回 (ret_code, (dataframe, last_page, all_count))
           - 旧代码 ret, data = result 导致 data 变成 3-tuple
           - 修复后：ret, (data, last_page, all_count) = result
           - 同步修复 _lazy_get_warrant_detail 中的同类问题
           - 增加 DataFrame 类型校验，杜绝 'tuple' has no 'iterrows'
  v4.6.5 - _lazy_get_warrant_detail 改用 WarrantRequest + get_warrant
  v4.6.4 - 稳定版
"""
import numpy as np
import pandas as pd
from scipy import stats
from typing import Optional, Dict, Tuple, List, Any
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
import time
import threading

log = logging.getLogger("RegimeAuto")

from core.kline_provider import KlineProvider, GlobalRateLimiter


class AdaptiveRegimePredictor:
    """
    自适应 Regime 预测器
    每只股票拥有独立的参数，基于自身历史数据自动计算
    """

    def __init__(self, quote_ctx=None, config: Optional[Dict] = None, db=None):
        self.q = quote_ctx
        self.db = db
        self.config = config or {}

        # 基础参数
        self.ma_period = 20
        self.adx_period = 14
        self.slope_window = 5
        self.history_len = 120
        self.min_history = 60
        self.iv_history_len = 504
        self.max_kline_pages = 3

        # 全局速率限制器（单例）
        self._rate_limiter = GlobalRateLimiter()

        # 引入 KlineProvider（类级缓存 + 限流 + 本地持久化）
        self._kline_provider = KlineProvider(
            quote_ctx=quote_ctx,
            market="HK",
            auto_type="QFQ",
            max_retries=3
        )

        # per-symbol 并发锁
        self._kline_locks: Dict[str, threading.Event] = {}
        self._kline_lock_main = threading.Lock()

        self._params_cache: Dict[str, Dict] = {}
        self.global_regime = "range_mid"
        self._regime_history: Dict[str, list] = defaultdict(list)
        self._cache: Dict[str, dict] = {}

        # 内存缓存
        self._underlying_cache: Dict[str, str] = {}
        self._warrant_detail_cache: Dict[str, dict] = {}
        self._cache_lock = threading.Lock()

        # 结构性产品识别规则
        self._structured_product_prefixes = ('5', '6')

        # 内置常用正股映射表（备用，当参数未传递时使用）
        self._builtin_underlying_map = {
            "52283": "HK.00700", "52811": "HK.00700", "52813": "HK.00700",
            "52256": "HK.00700", "52261": "HK.00700", "52532": "HK.00700",
            "54019": "HK.09988", "54038": "HK.09988", "54539": "HK.09988",
        }

    # ==================== 工具方法 ====================

    @staticmethod
    def _safe_df(data) -> Optional[pd.DataFrame]:
        """
        安全地将 get_warrant 返回的 data 部分转为 DataFrame。
        get_warrant 的内层返回可能是：
          - DataFrame（直接返回）
          - list of dict
          - 嵌套 tuple
          - None
        统一转为 DataFrame 或 None。
        """
        if data is None:
            return None
        if isinstance(data, pd.DataFrame):
            return data if not data.empty else None
        if isinstance(data, list):
            if len(data) == 0:
                return None
            if isinstance(data[0], dict):
                return pd.DataFrame(data)
            if isinstance(data[0], (list, tuple)):
                # 第一行是列名的情况
                if len(data) > 1:
                    return pd.DataFrame(data[1:], columns=data[0])
                return None
            return None
        if isinstance(data, tuple):
            # 递归尝试从 tuple 中提取 DataFrame
            for elem in data:
                df = AdaptiveRegimePredictor._safe_df(elem)
                if df is not None:
                    return df
            return None
        if isinstance(data, dict):
            return pd.DataFrame([data])
        return None

    # ==================== 主入口 ====================
    def predict(self, symbol: str, market: str = "US",
                underlying_symbol: Optional[str] = None) -> Dict:
        """预测单个标的的 Regime"""
        # 结构性产品快速路由
        if self._is_structured_product(symbol, market):
            log.info(f"[RegimeAuto] {symbol} 识别为结构性产品（窝轮/牛熊证）")
            if underlying_symbol is None:
                underlying_symbol = self._lazy_get_underlying(symbol)
            return self._predict_structured_product(symbol, market, underlying_symbol)

        # 普通标的：通过 KlineProvider 获取日K（带 per-symbol 锁）
        df = self._get_kline_with_lock(symbol)
        if df is None or len(df) < self.min_history:
            return self._fallback()

        close = df['close'].values.astype(float)
        high = df['high'].values.astype(float)
        low = df['low'].values.astype(float)

        thresholds = self._get_or_compute_thresholds(symbol, close)
        up_th = thresholds['up_threshold']
        down_th = thresholds['down_threshold']

        trend, trend_conf = self._judge_trend_adaptive(close, high, low, up_th, down_th)

        iv_percentile = self._get_iv_percentile(symbol, market, close)
        if iv_percentile is not None:
            vol_level, vol_conf = self._judge_vol_from_iv_adaptive(iv_percentile)
        else:
            vol_level, vol_conf = self._judge_vol_from_hv_adaptive(close)

        regime = f"{trend}_{vol_level}"
        confidence = round(min(trend_conf, vol_conf), 3)
        regime = self._stabilize(symbol, regime)
        probs = self._build_probs(trend, vol_level, confidence)

        factors = {
            'trend': round(float(thresholds.get('up_threshold', 0.01)), 4),
            'rsi': self._calc_rsi(close, 14),
            'volatility': round(float(np.std(close[-20:]) / np.mean(close[-20:])), 4),
            'volume_ratio': 1.0,
            'pos52': self._calc_52w_position(close),
        }

        result = {
            'regime': regime,
            'confidence': confidence,
            'factors': factors,
            'iv_percentile': iv_percentile,
            'probs': probs,
            'trend': trend,
            'vol_level': vol_level,
            'model': 'adaptive_v4',
        }

        self.global_regime = regime
        self._cache[symbol] = result
        log.info(f"[RegimeAuto] {symbol} → {regime} (conf={confidence:.2f}, iv_pct={iv_percentile})")
        return result

    # ==================== 结构性产品三级降级 ====================
    def _is_structured_product(self, symbol: str, market: str = "HK") -> bool:
        if market != "HK":
            return False
        clean = symbol.replace(".SEHK", "").replace("HK.", "")
        return clean.isdigit() and len(clean) == 5 and clean[0] in self._structured_product_prefixes

    def _lazy_get_underlying(self, derivative_symbol: str) -> Optional[str]:
        """
        通过富途查询窝轮/牛熊证对应的正股代码。
        ★ v4.6.6 修复：正确解包 get_warrant 的嵌套返回值。
        """
        deriv_code = derivative_symbol.replace(".SEHK", "").replace("HK.", "")
        if not deriv_code.startswith("HK."):
            deriv_code = f"HK.{deriv_code}"

        with self._cache_lock:
            if deriv_code in self._underlying_cache:
                return self._underlying_cache[deriv_code]

        clean_code = deriv_code.replace("HK.", "")
        if clean_code in self._builtin_underlying_map:
            owner = self._builtin_underlying_map[clean_code]
            with self._cache_lock:
                self._underlying_cache[deriv_code] = owner
            log.info(f"[RegimeAuto] 窝轮 {deriv_code} → 正股 {owner} (内置映射)")
            return owner

        if self.q is None:
            return None

        try:
            from futu import WrtType, WarrantRequest
            self._rate_limiter.acquire()
            req = WarrantRequest()
            req.num = 200
            req.type_list = [WrtType.CALL, WrtType.PUT, WrtType.BULL, WrtType.BEAR]
            begin = 0
            target_code = deriv_code.replace("HK.", "")

            while True:
                req.begin = begin

                # ★ 修复：get_warrant 返回 (ret_code, (dataframe, last_page, all_count))
                # 必须用嵌套解包，否则 data 会变成 3-tuple
                result = self.q.get_warrant("", req)

                ret = None
                data = None
                last_page = True
                all_count = 0

                if isinstance(result, tuple):
                    if len(result) == 2:
                        # (ret_code, inner_tuple)
                        ret, inner = result
                        data = self._safe_df(inner)
                        last_page = True
                    elif len(result) == 3:
                        # (ret_code, dataframe, page_req_key) — 某些版本
                        ret, raw_data, _ = result
                        data = self._safe_df(raw_data)
                        last_page = (_ is None)
                    else:
                        log.warning(f"[RegimeAuto] get_warrant 返回格式异常 len={len(result)}")
                        break
                else:
                    log.warning(f"[RegimeAuto] get_warrant 返回非 tuple: {type(result)}")
                    break

                # ★ 确保 data 是 DataFrame（_safe_df 已保证）
                if ret != 0 or data is None or len(data) == 0:
                    break

                # ★ 现在 data 一定是 DataFrame，iterrows 安全
                for _, row in data.iterrows():
                    stock_val = row.get('stock', '')
                    if stock_val == deriv_code or stock_val == target_code:
                        owner = row.get('stock_owner', '')
                        if owner:
                            if not owner.startswith("HK."):
                                owner = f"HK.{owner.replace('HK.', '').replace('.HK', '')}"
                            with self._cache_lock:
                                self._underlying_cache[deriv_code] = owner
                            log.info(f"[RegimeAuto] 窝轮 {deriv_code} → 正股 {owner}")
                            return owner

                all_count = len(data)
                begin += req.num
                if last_page or begin >= all_count:
                    break
                time.sleep(0.2)

            log.warning(f"[RegimeAuto] 未找到窝轮 {deriv_code} 的正股映射")
            return None

        except Exception as e:
            log.error(f"[RegimeAuto] 查询窝轮正股异常 {deriv_code}: {e}")
            return None

    def _lazy_get_warrant_detail(self, symbol: str,
                                   underlying: Optional[str] = None) -> Optional[dict]:
        """查询窝轮/牛熊证详细数据（支持全局搜索兜底）"""
        deriv_code = symbol.replace(".SEHK", "").replace("HK.", "").replace("hk.", "")
        target_stock = f"HK.{deriv_code}"

        with self._cache_lock:
            if symbol in self._warrant_detail_cache:
                return self._warrant_detail_cache[symbol]

        if self.q is None:
            return None

        from futu import WarrantRequest, SortField, WrtType, RET_OK

        total_checked = 0
        max_pages = 10

        def _search_by_owner(owner: str) -> Optional[dict]:
            nonlocal total_checked
            for page in range(max_pages):
                req = WarrantRequest()
                req.sort_field = SortField.TURNOVER
                req.type_list = [WrtType.CALL, WrtType.PUT, WrtType.BULL, WrtType.BEAR]
                req.num = 200
                req.begin = page * 200

                for attempt in range(3):
                    self._rate_limiter.acquire()
                    try:
                        # ★ 修复：嵌套解包 + _safe_df
                        result = self.q.get_warrant(owner, req)
                        if isinstance(result, tuple) and len(result) >= 2:
                            ret = result[0]
                            raw = result[1]
                            # 如果 result[1] 本身是 (df, last_page, all_count)
                            if isinstance(raw, tuple) and len(raw) >= 1:
                                df = self._safe_df(raw[0])
                            else:
                                df = self._safe_df(raw)
                        else:
                            ret, df = RET_OK, None
                            log.warning(f"[RegimeAuto] get_warrant 格式异常 owner={owner}")
                    except ValueError:
                        # 旧版 API 兼容
                        ret, raw = self.q.get_warrant(owner, req)
                        df = self._safe_df(raw)

                    if ret == RET_OK and df is not None:
                        break
                    elif attempt < 2:
                        log.warning(f"[RegimeAuto] get_warrant ret={ret}, 重试 {attempt+1}/3")
                        time.sleep(0.5)
                    else:
                        log.warning(f"[RegimeAuto] get_warrant 失败 owner={owner}, ret={ret}")
                        return None

                if df is None or df.empty:
                    break

                if page == 0 and 'stock' in df.columns:
                    sample = df['stock'].head(5).tolist()
                    log.info(f"[RegimeAuto] 正股 {owner} 下窝轮 stock 格式样例: {sample}")

                if 'stock' in df.columns:
                    matches = df[df['stock'] == target_stock]
                    if len(matches) > 0:
                        return matches.iloc[0].to_dict()

                total_checked += len(df)
                if len(df) < req.num:
                    break
                time.sleep(0.05)
            return None

        # 第一步：用内置映射的正股搜索
        if underlying is None:
            underlying = self._lazy_get_underlying(symbol)
        found = None
        if underlying:
            found = _search_by_owner(underlying)
            if found:
                log.debug(f"[RegimeAuto] ✅ 在正股 {underlying} 中找到 {symbol}")
            else:
                log.warning(f"[RegimeAuto] 在正股 {underlying} 中未找到 {symbol}，尝试全局搜索...")

        # 第二步：全局搜索（空owner）
        if not found:
            total_checked = 0
            found = _search_by_owner("")
            if found:
                actual_underlying = found.get('stock_owner', '')
                if actual_underlying:
                    with self._cache_lock:
                        self._underlying_cache[symbol] = actual_underlying
                    log.info(f"[RegimeAuto] ✅ 全局搜索找到 {symbol}，正股为 {actual_underlying}，已更新映射")

        if not found:
            log.warning(f"[RegimeAuto] 未找到窝轮 {symbol} (checked={total_checked})")
            return None

        detail = {
            'implied_volatility': found.get('implied_volatility'),
            'street_rate': found.get('street_rate', 0),
            'volume': found.get('volume', 0),
            'delta': found.get('delta'),
            'effective_leverage': found.get('effective_leverage', 0),
            'price_recovery_ratio': found.get('price_recovery_ratio'),
            'leverage': found.get('leverage', 0),
            'premium': found.get('premium', 0),
            'strike_price': found.get('strike_price', 0),
            'recovery_price': found.get('recovery_price'),
        }

        with self._cache_lock:
            self._warrant_detail_cache[symbol] = detail
        log.debug(f"[RegimeAuto] ✅ 获取窝轮详情 {symbol} (via get_warrant)")
        return detail

    # ==================== 结构化产品 regime ====================
    def _predict_structured_product(self, symbol: str, market: str = "HK",
                                    underlying_symbol: Optional[str] = None) -> Dict:
        trend = "range"
        trend_conf = 0.3
        l1_ok = False

        if underlying_symbol is not None and not self._is_structured_product(underlying_symbol, market):
            try:
                underlying_regime = self.predict(underlying_symbol, market="HK")
                if underlying_regime.get("model") != "fallback":
                    trend = underlying_regime.get("trend", "range")
                    trend_conf = underlying_regime.get("confidence", 0.3) * 0.8
                    l1_ok = True
                    log.info(f"[RegimeAuto] {symbol} L1: 使用正股 {underlying_symbol} regime")
            except Exception as e:
                log.debug(f"[RegimeAuto] {symbol} L1失败: {e}")

        warrant_data = self._lazy_get_warrant_detail(symbol, underlying_symbol)
        vol_level = "mid"
        vol_conf = 0.5

        if warrant_data is not None:
            implied_vol = warrant_data.get('implied_volatility', None)
            street_rate = warrant_data.get('street_rate', 0)
            volume = warrant_data.get('volume', 0)
            effective_leverage = warrant_data.get('effective_leverage', 0)
            delta = warrant_data.get('delta', None)
            price_recovery_ratio = warrant_data.get('price_recovery_ratio', None)

            if implied_vol is not None:
                if implied_vol > 40:
                    vol_level = "high"; vol_conf = 0.7
                elif implied_vol > 25:
                    vol_level = "mid"; vol_conf = 0.6
                else:
                    vol_level = "low"; vol_conf = 0.6
            elif price_recovery_ratio is not None:
                if price_recovery_ratio < 5:
                    vol_level = "high"; vol_conf = 0.7
                elif price_recovery_ratio < 15:
                    vol_level = "mid"; vol_conf = 0.6
                else:
                    vol_level = "low"; vol_conf = 0.6
            else:
                if volume > 500000:
                    vol_level = "high"; vol_conf = 0.6
                elif volume > 100000:
                    vol_level = "mid"; vol_conf = 0.5
                else:
                    vol_level = "low"; vol_conf = 0.5

            if not l1_ok:
                if delta is not None:
                    if delta > 0.6:
                        trend = "up"; trend_conf = min(delta, 0.6)
                    elif delta < 0.4:
                        trend = "down"; trend_conf = min(1 - delta, 0.6)
                    else:
                        trend = "range"; trend_conf = 0.3
                elif street_rate > 80:
                    trend = "up"; trend_conf = 0.4
                elif street_rate < 20:
                    trend = "down"; trend_conf = 0.4
                else:
                    trend = "range"; trend_conf = 0.3
        else:
            # 无窝轮详情时，用正股 regime 兜底
            if underlying_symbol:
                try:
                    ur = self.predict(underlying_symbol, market="HK")
                    trend = ur.get("trend", "range")
                    trend_conf = ur.get("confidence", 0.3) * 0.7
                except Exception:
                    pass

        regime = f"{trend}_{vol_level}"
        confidence = round(min(trend_conf, vol_conf), 3)

        result = {
            'regime': regime,
            'confidence': confidence,
            'factors': {
                'trend': trend_conf,
                'volatility': 0.5,
                'volume_ratio': 1.0,
                'delta': warrant_data.get('delta') if warrant_data else None,
                'implied_vol': warrant_data.get('implied_volatility') if warrant_data else None,
            },
            'iv_percentile': None,
            'probs': self._build_probs(trend, vol_level, confidence),
            'trend': trend,
            'vol_level': vol_level,
            'model': 'adaptive_v4_structured',
        }
        self._cache[symbol] = result
        log.info(f"[RegimeAuto] {symbol} → {regime} (conf={confidence:.2f}, structured)")
        return result

    # ==================== 衍生品 regime ====================
    def predict_derivative(self, underlying_symbol: str,
                           derivative_type: str = "OPTION",
                           derivative_symbol: Optional[str] = None) -> dict:
        if derivative_symbol and self._is_structured_product(derivative_symbol):
            base = self._predict_structured_product(derivative_symbol, "HK")
        else:
            base = self.predict(underlying_symbol)

        regime = base.get("regime", "range_mid")
        confidence = base.get("confidence", 0.5)
        iv_pct = base.get("iv_percentile", 0.5)

        if derivative_type == "OPTION":
            if iv_pct > 70 and "range" in regime:
                regime = "range_high_iv"
            elif iv_pct < 30 and "volatile" in regime:
                regime = "volatile_low_iv"
        elif derivative_type == "CBBC":
            if "bull" not in regime and "bear" not in regime:
                regime = "range_mid"
        elif derivative_type == "WARRANT":
            if ("range" in regime or "volatile" in regime) and confidence < 0.6:
                regime = "range_mid"

        return {
            "regime": regime,
            "confidence": round(confidence * 0.9, 2),
            "underlying_regime": base.get("regime", "range_mid"),
            "iv_percentile": iv_pct,
        }

    # ==================== K线获取 ====================
    def _get_kline_with_lock(self, symbol: str):
        """带 per-symbol 锁的 K线获取"""
        if symbol in self._kline_locks:
            event = self._kline_locks[symbol]
        else:
            with self._kline_lock_main:
                if symbol not in self._kline_locks:
                    self._kline_locks[symbol] = threading.Event()
                event = self._kline_locks[symbol]

        if event.is_set():
            # 另一个线程已完成，直接从 KlineProvider 取缓存
            return self._kline_provider.get_daily(symbol, days=self.history_len)

        try:
            df = self._kline_provider.get_daily(symbol, days=self.history_len)
            return df
        finally:
            event.set()
            with self._kline_lock_main:
                self._kline_locks.pop(symbol, None)

    # 保留旧接口兼容
    def _get_kline(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._get_kline_with_lock(symbol)

    # ==================== 阈值计算 ====================
    def _get_or_compute_thresholds(self, symbol, close):
        cache_key = f"{symbol}_{len(close)}"
        if cache_key in self._params_cache:
            return self._params_cache[cache_key]
        if len(close) < self.ma_period + self.slope_window + 10:
            thresholds = {'up_threshold': 0.010, 'down_threshold': -0.010}
        else:
            ma = pd.Series(close).rolling(self.ma_period).mean().values
            valid_ma = ma[self.ma_period:]
            valid_close = close[self.ma_period:]
            if len(valid_ma) < self.slope_window:
                thresholds = {'up_threshold': 0.010, 'down_threshold': -0.010}
            else:
                recent_ma = valid_ma[-self.slope_window:]
                slope = (recent_ma[-1] - recent_ma[0]) / max(abs(recent_ma[0]), 1e-10)
                std = np.std(valid_close[-self.ma_period:])
                avg_price = np.mean(valid_close[-self.ma_period:])
                normalized_std = std / max(avg_price, 1e-10)
                up_t = 0.008 + 0.005 * min(abs(slope) * 10, 1.0) + normalized_std * 0.3
                down_t = -0.008 - 0.005 * min(abs(slope) * 10, 1.0) - normalized_std * 0.3
                thresholds = {'up_threshold': up_t, 'down_threshold': down_t}
        self._params_cache[cache_key] = thresholds
        return thresholds

    # ==================== 趋势判断 ====================
    def _judge_trend_adaptive(self, close, high, low, up_th, down_th):
        """基于 MA 斜率 + ADX 的自适应趋势判断"""
        if len(close) < self.ma_period + self.slope_window:
            return "range", 0.3

        ma = pd.Series(close).rolling(self.ma_period).mean().values
        valid_ma = ma[self.ma_period:]
        if len(valid_ma) < self.slope_window:
            return "range", 0.3

        # MA 斜率
        recent_ma = valid_ma[-self.slope_window:]
        slope = (recent_ma[-1] - recent_ma[0]) / max(abs(recent_ma[0]), 1e-10)

        # 价格位置
        last_close = close[-1]
        ma_now = ma[-1] if not np.isnan(ma[-1]) else last_close
        price_diff = (last_close - ma_now) / max(abs(ma_now), 1e-10)

        # ADX
        try:
            adx = self._calc_adx(high, low, close, self.adx_period)
        except Exception:
            adx = 20.0

        # 综合判断
        if slope > up_th and price_diff > 0.02 and adx > 25:
            return "up", min(0.3 + abs(slope) * 5 + adx / 100, 0.9)
        elif slope < down_th and price_diff < -0.02 and adx > 25:
            return "down", min(0.3 + abs(slope) * 5 + adx / 100, 0.9)
        else:
            return "range", min(0.2 + adx / 200, 0.5)

    # ==================== ADX 计算 ====================
    def _calc_adx(self, high, low, close, period: int = 14) -> float:
        """计算 ADX 指标"""
        if len(close) < period + 1:
            return 20.0
        highs = np.array(high[-(period+1):])
        lows = np.array(low[-(period+1):])
        closes = np.array(close[-(period+1):])

        up = highs[1:] - highs[:-1]
        down = lows[:-1] - lows[1:]
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = np.maximum.reduce([
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1])
        ])
        atr = pd.Series(tr).rolling(period).mean().values
        valid = ~np.isnan(atr) & (atr > 0)
        if not np.any(valid):
            return 20.0
        pdi = 100 * pd.Series(plus_dm).rolling(period).mean().values / atr
        mdi = 100 * pd.Series(minus_dm).rolling(period).mean().values / atr
        dx = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-10)
        adx = pd.Series(dx).rolling(period).mean().values
        valid_adx = adx[~np.isnan(adx)]
        return float(valid_adx[-1]) if len(valid_adx) > 0 else 20.0

    # ==================== IV 百分位 ====================
    def _get_iv_percentile(self, symbol: str, market: str, close) -> Optional[float]:
        """获取 IV 百分位（期权标的）"""
        # 简化实现：基于历史波动率估算
        if len(close) < 30:
            return None
        returns = np.diff(np.log(close[-30:]))
        hv = np.std(returns) * np.sqrt(252) * 100
        # 归一化到 0-100
        iv_pct = max(0, min(100, (hv - 10) / 40 * 100))
        return round(iv_pct, 1)

    def _judge_vol_from_iv_adaptive(self, iv_percentile: float):
        """基于 IV 百分位判断波动率水平"""
        if iv_percentile > 70:
            return "high", 0.7
        elif iv_percentile > 40:
            return "mid", 0.6
        elif iv_percentile > 20:
            return "low", 0.5
        else:
            return "low", 0.4

    def _judge_vol_from_hv_adaptive(self, close):
        """基于历史波动率判断（IV 不可用时的兜底）"""
        if len(close) < 20:
            return "mid", 0.4
        rets = np.diff(np.log(close[-20:]))
        hv = np.std(rets) * np.sqrt(252)
        if hv > 0.4:
            return "high", 0.6
        elif hv > 0.2:
            return "mid", 0.5
        else:
            return "low", 0.4

    # ==================== RSI ====================
    def _calc_rsi(self, close, period: int = 14) -> float:
        if len(close) < period + 1:
            return 50.0
        deltas = np.diff(close[-(period+1):])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 2)

    # ==================== 52周位置 ====================
    def _calc_52w_position(self, close) -> float:
        if len(close) < 252:
            window = close[-min(len(close), 60):]
        else:
            window = close[-252:]
        if len(window) == 0:
            return 0.5
        high = max(window)
        low = min(window)
        cur = close[-1]
        if high == low:
            return 0.5
        return round((cur - low) / (high - low), 4)

    # ==================== 概率分布 ====================
    def _build_probs(self, trend: str, vol_level: str, confidence: float) -> dict:
        """构建 regime 概率分布"""
        base = {"trend": 0.33, "range": 0.34, "volatile": 0.33}
        if trend == "up":
            base["trend"] = 0.5
            base["range"] = 0.3
            base["volatile"] = 0.2
        elif trend == "down":
            base["trend"] = 0.2
            base["range"] = 0.3
            base["volatile"] = 0.5
        else:
            base["trend"] = 0.25
            base["range"] = 0.5
            base["volatile"] = 0.25
        # 用 confidence 调整
        c = min(confidence, 0.9)
        dominant = max(base, key=base.get)
        base[dominant] = c
        others = [k for k in base if k != dominant]
        remaining = 1 - c
        for o in others:
            base[o] = round(remaining / len(others), 4)
        base[dominant] = round(1 - sum(base[o] for o in others), 4)
        return base

    # ==================== 稳定化 ====================
    def _stabilize(self, symbol: str, regime: str) -> str:
        """对 regime 做简单平滑，避免频繁跳变"""
        history = self._regime_history.get(symbol, [])
        history.append(regime)
        if len(history) > 5:
            history = history[-5:]
        self._regime_history[symbol] = history
        # 如果最近3次有超过半数相同，使用多数
        if len(history) >= 3:
            recent = history[-3:]
            from collections import Counter
            most_common, count = Counter(recent).most_common(1)[0]
            if count >= 2:
                return most_common
        return regime

    # ==================== 降级 ====================
    def _fallback(self) -> dict:
        """数据不足时的降级输出"""
        return {
            'regime': 'range_mid',
            'confidence': 0.2,
            'factors': {'trend': 0.01, 'rsi': 50, 'volatility': 0.02, 'volume_ratio': 1.0, 'pos52': 0.5},
            'iv_percentile': None,
            'probs': {'trend': 0.33, 'range': 0.34, 'volatile': 0.33},
            'trend': 'range',
            'vol_level': 'mid',
            'model': 'fallback',
        }

    # ==================== 批量预测 ====================
    def batch_predict(self, symbols: List[Tuple[str, str]], max_workers: int = 2) -> Dict[str, Dict]:
        """批量预测，默认最大并发数为2（避免触发富途速率限制）"""
        results = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self.predict, sym, mkt): sym for sym, mkt in symbols}
            for f in as_completed(futures):
                sym = futures[f]
                try:
                    results[sym] = f.result()
                except Exception as e:
                    log.error(f"[RegimeAuto] 预测 {sym} 失败: {e}")
                    results[sym] = self._fallback()
        return results
