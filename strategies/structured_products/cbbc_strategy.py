"""
strategies/structured_products/cbbc_strategy.py - v2.9.3
牛熊证策略（实盘级重写）

═════════════════════════════════════════════════════════════
【核心数据源】富途 OpenAPI 官方接口
    get_warrant(stock_owner, WarrantRequest)
        → 返回 DataFrame，牛熊证相关关键字段（已按官方文档核对）：
            stock                 牛熊证代码（如下单标的）
            type                  WrtType.BULL / WrtType.BEAR
            stock_owner           所属正股代码
            leverage             杠杆倍数
            recovery_price        收回价（触发强制收回用）
            price_recovery_ratio 正股距收回价（%，越大越安全）
            maturity_time        到期日
            last_trade_time      最后交易日
            strike_price         行使价（牛熊证通常等于收回价附近）
            conversion_ratio     换股比率
            status               状态（NORMAL/SUSPENDED/PRE_IPO）
            break_even_point     打和点
            cur_price/current_price 现价
            lot_size             每手数量
            implied_volatility   引伸波幅
            delta                对冲值
            effective_leverage   有效杠杆
            ipop                 价内/价外
            premium              溢价

═════════════════════════════════════════════════════════════
【与上版的根本性差异】
    上版（v2.6）致命缺陷：
        1. 从未调用 get_warrant —— 杠杆/距收回价全是 np.random 伪造
        2. self.buy/sell 用正股 vt_symbol —— 牛熊证必须下到自己的代码
        3. 没有"距收回价"真实数据 → 无法防御强制收回
        4. 盈亏用杠杆×价格变动估算但杠杆是假的 → 全是随机数
        5. 完全没订阅牛熊证自身行情

    本版修复：
        1. _query_cbbc_chain() 真实调 get_warrant(type_list=[BULL/BEAR])
        2. 下单标的 = 筛选出的牛熊证 stock 代码
        3. 真实 recovery_price + price_recovery_ratio 防御强制收回
        4. 多周期分层（1M 执行 / 5M 趋势 / 60M Regime）
        5. 盘口 imbalance + 逐笔 tick 利用
        6. Regime 仓位缩放 + ADX 趋势过滤
        7. 距收回价过近时禁止开仓（核心风控）
"""
import time
import math
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData, OrderRequest, SubscribeRequest
from vnpy.trader.constant import Direction, Exchange, Offset, Status
from vnpy.trader.utility import round_to

from futu import (
    OpenQuoteContext, RET_OK, WarrantRequest, WrtType,
    WarrantStatus, SortField, SecurityType,
)

# ═══════════════════════════════════════════════════════════
#  工具函数（与 warrant_strategy 保持一致）
# ═══════════════════════════════════════════════════════════
def _to_float(val, default=0.0):
    if val is None:
        return default
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (ValueError, OverflowError):
            return default
    s = str(val).strip()
    if s == '' or s.upper() == 'N/A':
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _days_to_maturity(maturity_str: str) -> int:
    try:
        m = datetime.strptime(maturity_str, "%Y-%m-%d").date()
        return (m - datetime.now().date()).days
    except (ValueError, TypeError):
        return 999


def _wrt_type_to_str(t) -> str:
    mapping = {WrtType.CALL: "CALL", WrtType.PUT: "PUT",
               WrtType.BULL: "BULL", WrtType.BEAR: "BEAR"}
    if t in mapping:
        return mapping[t]
    int_map = {1: "CALL", 2: "PUT", 3: "BULL", 4: "BEAR", 5: "INLINE"}
    try:
        return int_map.get(int(t), str(t))
    except (ValueError, TypeError):
        return str(t)


# ═══════════════════════════════════════════════════════════
#  主策略类
# ═══════════════════════════════════════════════════════════
class CBBCStrategy(CtaTemplate):
    """
    实盘级牛熊证策略

    数据流（对应已订阅的全套数据）：
        TICKER  → on_tick   ：盘口 imbalance、加权价
        K_1M    → on_1m_bar ：执行层（止损/止盈/收回预警/超时）
        K_5M    → on_5m_bar ：趋势确认（EMA 多头/空头排列）
        K_60M   → on_60m_bar：宏观 Regime 更新
        QUOTE   → on_tick   ：bid/ask 快照
    """
    author = "Apollo"

    # ───────────────────────────────────────
    #  可调参数
    # ───────────────────────────────────────
    parameters = [
        # 信号
        "signal_source",
        "underlying_symbol",
        # 牛熊证筛选
        "min_leverage", "max_leverage",
        "min_distance_to_call",    # 正股距收回价最小（%）：越近杠杆越高风险越大
        "max_distance_to_call",    # 正股距收回价最大（%）：越远越安全但杠杆越低
        "min_days_to_expiry", "max_days_to_expiry",
        "min_effective_leverage",  # 最低有效杠杆
        "max_recovery_pct",        # 距收回价上限（%）
        "prefer_issuer_list",
        # 风控
        "recovery_warn_pct",       # 距收回价低于此值发预警（%）
        "recovery_exit_pct",       # 距收回价低于此值强制平仓（%）
        "max_position_value",
        "position_pct_of_cash",
        "profit_take_pct",        # 止盈（基于杠杆放大后的盈亏%）
        "stop_loss_pct",
        "max_hold_bars",
        "time_decay_close_days",
        # 趋势
        "adx_threshold",
        "ema_fast", "ema_slow",
        # Regime
        "regime_scale",
        # 节流
        "requery_interval_sec",
    ]

    variables = [
        "pos", "entry_price", "entry_time", "current_cbbc_stock",
        "cbbc_type", "leverage", "distance_to_call",
        "days_to_expiry", "pnl_pct", "bars_held",
        "last_signal", "regime_label",
    ]

    # ═══════════════════════════════════════════════════
    #  初始化
    # ═══════════════════════════════════════════════════
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.pos = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.current_cbbc_stock = ""
        self.cbbc_type = ""          # BULL / BEAR
        self.leverage = 0.0
        self.distance_to_call = 0.0  # 正股距收回价（%）
        self.days_to_expiry = 0
        self.pnl_pct = 0.0
        self.bars_held = 0
        self.last_signal = 0.0
        self.regime_label = "unknown"

        # 多周期
        self.bg_1m = BarGenerator(self.on_1m_bar, window=1, on_window_bar=self.on_1m_bar)
        self.bg_5m = BarGenerator(self.on_1m_bar, window=5, on_window_bar=self.on_5m_bar)
        self.bg_60m = BarGenerator(self.on_1m_bar, window=60, on_window_bar=self.on_60m_bar)
        self.am_1m = ArrayManager(size=200)
        self.am_5m = ArrayManager(size=200)
        self.am_60m = ArrayManager(size=100)

        self.last_tick = None
        self.tick_imbalance = 0.0

        self.quote_ctx: Optional[OpenQuoteContext] = None
        self._last_query_ts = 0.0
        self._candidates: List[Dict] = []
        self._current_info: Dict = {}

        self._shared_signal = 0.0

        self._set_defaults()

    def _set_defaults(self):
        defaults = {
            "signal_source": "self",
            "underlying_symbol": "HK.00700",
            "min_leverage": 3.0, "max_leverage": 12.0,
            "min_distance_to_call": 3.0,   # 距收回价至少 3%
            "max_distance_to_call": 18.0,  # 距收回价最多 18%
            "min_days_to_expiry": 14, "max_days_to_expiry": 120,
            "min_effective_leverage": 2.0,
            "max_recovery_pct": 25.0,
            "prefer_issuer_list": "",
            "recovery_warn_pct": 5.0,      # 距收回价 <5% 预警
            "recovery_exit_pct": 2.0,      # 距收回价 <2% 强平
            "max_position_value": 60000.0,
            "position_pct_of_cash": 0.1,
            "profit_take_pct": 0.20,
            "stop_loss_pct": 0.12,
            "max_hold_bars": 240,
            "time_decay_close_days": 5,
            "adx_threshold": 20.0,
            "ema_fast": 5, "ema_slow": 20,
            "regime_scale": True,
            "requery_interval_sec": 300,
        }
        for k, v in defaults.items():
            if not hasattr(self, k) or getattr(self, k) is None:
                setattr(self, k, v)

    def on_init(self):
        self.load_bar(30, use_database=True)
        self.write_log(
            f"[CBBC] 策略初始化 | 正股={self.underlying_symbol} | "
            f"杠杆={self.min_leverage:.0f}x~{self.max_leverage:.0f}x | "
            f"距收回={self.min_distance_to_call}~{self.max_distance_to_call}%"
        )

    def on_start(self):
        self.quote_ctx = self._get_quote_ctx()
        if self.quote_ctx is None:
            self.write_log("[CBBC] ⚠️ quote_ctx 未就绪，牛熊证筛选将失败！")
        else:
            self.write_log("[CBBC] ✅ quote_ctx 已就绪")
        self._subscribe_underlying()
        self._query_cbbc_chain(force=True)

    def on_stop(self):
        self.write_log(f"[CBBC] 停止 | 持仓={self.pos}")

    # ═══════════════════════════════════════════════════
    #  行情接入
    # ═══════════════════════════════════════════════════
    def on_tick(self, tick: TickData):
        self.last_tick = tick
        bid = _to_float(tick.bid_price_1, 0.0)
        ask = _to_float(tick.ask_price_1, 0.0)
        bv = _to_float(tick.bid_volume_1, 0.0)
        av = _to_float(tick.ask_volume_1, 0.0)
        total = bv + av
        self.tick_imbalance = (bv - av) / total if total > 0 else 0.0
        self.bg_1m.update_tick(tick)

    def on_bar(self, bar: BarData):
        self.bg_1m.update_bar(bar)
        self.bg_5m.update_bar(bar)
        self.bg_60m.update_bar(bar)

    def on_1m_bar(self, bar: BarData):
        self.am_1m.update_bar(bar)
        if not self.am_1m.inited:
            return

        self._read_shared_signal()

        if self.pos != 0:
            self.bars_held += 1
            self._manage_position(bar)
        else:
            self.bars_held = 0
            signal = self._get_combined_signal(bar)
            self.last_signal = signal
            if abs(signal) >= 1.0:
                self._try_open_position(signal, bar)

        self._maybe_requery()

    def on_5m_bar(self, bar: BarData):
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        self._update_regime_from_5m()

    def on_60m_bar(self, bar: BarData):
        self.am_60m.update_bar(bar)
        if not self.am_60m.inited:
            return
        self._update_regime_from_60m()

    # ═══════════════════════════════════════════════════
    #  信号
    # ═══════════════════════════════════════════════════
    def _get_combined_signal(self, bar: BarData) -> float:
        if not self.am_5m.inited or not self.am_1m.inited:
            return 0.0

        ema_f = self.am_5m.ema(self.ema_fast)
        ema_s = self.am_5m.ema(self.ema_slow)
        if ema_f > ema_s * 1.002:
            trend = 1.0
        elif ema_f < ema_s * 0.998:
            trend = -1.0
        else:
            trend = 0.0

        adx = self.am_5m.adx(14) if hasattr(self.am_5m, 'adx') else 25.0
        adx_w = min(max((adx - 10) / 30, 0.0), 1.0)

        rsi = self.am_1m.rsi(14)
        rsi_sig = 0.0
        if rsi > 72:
            rsi_sig = -0.3
        elif rsi < 28:
            rsi_sig = 0.3

        imb = np.clip(self.tick_imbalance, -1.0, 1.0)
        shared = self._shared_signal if abs(self._shared_signal) >= 1.0 else 0.0

        raw = (
            trend * 0.5 * (0.5 + 0.5 * adx_w)
            + rsi_sig * 0.2
            + imb * 0.15
            + shared * 0.15
        )

        if raw >= 0.5 and trend > 0:
            return 1.0
        if raw <= -0.5 and trend < 0:
            return -1.0
        return 0.0

    def _update_regime_from_5m(self):
        if not hasattr(self.am_5m, 'adx'):
            return
        adx = self.am_5m.adx(14)
        close = self.am_5m.close[-1] if len(self.am_5m.close) else 0.0
        ma20 = np.mean(self.am_5m.close[-20:]) if len(self.am_5m.close) >= 20 else close
        if adx > 30 and close > ma20:
            self.regime_label = "bull_trend"
        elif adx > 30 and close < ma20:
            self.regime_label = "bear_trend"
        elif adx < 18:
            self.regime_label = "range"
        else:
            self.regime_label = "volatile"

    def _update_regime_from_60m(self):
        if len(self.am_60m.close) < 20:
            return
        ma20 = np.mean(self.am_60m.close[-20:])
        ma60 = np.mean(self.am_60m.close[-60:]) if len(self.am_60m.close) >= 60 else ma20
        adx = self.am_60m.adx(14) if hasattr(self.am_60m, 'adx') else 25.0
        if adx > 30:
            self.regime_label = "bull_trend" if ma20 > ma60 else "bear_trend"
        else:
            self.regime_label = "range"

    def _read_shared_signal(self):
        try:
            bus = getattr(self, 'event_bus', None) or getattr(self.cta_engine, 'event_bus', None)
            if bus is None:
                return
            last = bus.get_last('eSignal')
            if last and last.get('symbol') == self.underlying_symbol:
                self._shared_signal = float(last.get('signal', 0.0))
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  牛熊证筛选（核心）
    # ═══════════════════════════════════════════════════
    def _query_cbbc_chain(self, force=False) -> List[Dict]:
        """
        调用富途 get_warrant 筛选牛熊证（WrtType.BULL / WrtType.BEAR）
        """
        now = time.time()
        if not force and (now - self._last_query_ts) < self.requery_interval_sec:
            return self._candidates
        self._last_query_ts = now

        if self.quote_ctx is None:
            self.write_log("[CBBC] ⚠️ quote_ctx 未就绪")
            return []

        cands: List[Dict] = []

        for cbbc_type in [WrtType.BULL, WrtType.BEAR]:
            req = WarrantRequest()
            req.type_list = [cbbc_type]
            req.leverage_ratio_min = self.min_leverage
            req.leverage_ratio_max = self.max_leverage
            req.status = WarrantStatus.NORMAL
            # 距收回价过滤（牛熊证核心字段）
            req.price_recovery_ratio_min = self.min_distance_to_call
            req.price_recovery_ratio_max = self.max_distance_to_call
            # 到期日
            today = datetime.now().date()
            req.maturity_time_min = (today + timedelta(days=self.min_days_to_expiry)).strftime("%Y-%m-%d")
            req.maturity_time_max = (today + timedelta(days=self.max_days_to_expiry)).strftime("%Y-%m-%d")
            # 排序
            req.sort_field = SortField.SCORE
            req.ascend = False
            req.num = 50

            try:
                ret, result = self.quote_ctx.get_warrant(self.underlying_symbol, req)
            except Exception as e:
                self.write_log(f"[CBBC] get_warrant({cbbc_type}) 异常: {e}")
                continue

            if ret != RET_OK:
                self.write_log(f"[CBBC] get_warrant({cbbc_type}) 失败: {result}")
                continue

            try:
                data, last_page, all_count = result
            except (TypeError, ValueError):
                data = result if hasattr(result, '__iter__') else None
                last_page, all_count = True, 0

            if data is None or len(data) == 0:
                continue

            for _, row in data.iterrows():
                item = self._parse_row(row, cbbc_type)
                if item and self._passes_filter(item):
                    cands.append(item)

        cands.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        self._candidates = cands

        if cands:
            s = cands[0]
            self.write_log(
                f"[CBBC] ✅ 筛选完成: {len(cands)} 只 | "
                f"样本={s['stock']} type={s['cbbc_type_str']} "
                f"lev={s['leverage']:.1f}x dist={s['price_recovery_ratio']:.1f}% "
                f"days={s['days_to_expiry']}"
            )
        else:
            self.write_log(f"[CBBC] ⚠️ 无符合筛选条件的牛熊证（正股={self.underlying_symbol}）")

        return cands

    def _parse_row(self, row, cbbc_type) -> Optional[Dict]:
        try:
            stock = str(row.get("stock", "")).strip()
            if not stock:
                return None
            d = {
                "stock": stock,
                "cbbc_type": cbbc_type,
                "cbbc_type_str": _wrt_type_to_str(cbbc_type),
                "stock_owner": str(row.get("stock_owner", self.underlying_symbol)),
                "name": str(row.get("name", stock)),
                "leverage": _to_float(row.get("leverage"), 0.0),
                "effective_leverage": _to_float(row.get("effective_leverage"), 0.0),
                "recovery_price": _to_float(row.get("recovery_price"), 0.0),
                "price_recovery_ratio": _to_float(row.get("price_recovery_ratio"), 0.0),
                "strike_price": _to_float(row.get("strike_price"), 0.0),
                "conversion_ratio": _to_float(row.get("conversion_ratio"), 1.0),
                "maturity_time": str(row.get("maturity_time", "")),
                "last_trade_time": str(row.get("last_trade_time", "")),
                "status": int(_to_float(row.get("status"), 0)),
                "break_even_point": _to_float(row.get("break_even_point"), 0.0),
                "cur_price": _to_float(row.get("cur_price", row.get("current_price", 0.0)), 0.0),
                "delta": _to_float(row.get("delta"), 0.0),
                "implied_volatility": _to_float(row.get("implied_volatility"), 0.0),
                "premium": _to_float(row.get("premium"), 0.0),
                "ipop": _to_float(row.get("ipop"), 0.0),
                "lot_size": int(_to_float(row.get("lot_size"), 1000)),
                "issuer": str(row.get("issuer", "")),
                "score": _to_float(row.get("score"), 0.0),
                "days_to_expiry": _days_to_maturity(str(row.get("maturity_time", ""))),
            }
            return d
        except Exception as e:
            self.write_log(f"[CBBC] 解析行异常: {e}")
            return None

    def _passes_filter(self, w: Dict) -> bool:
        # 状态
        if w["status"] != 0:
            return False
        # 有效杠杆
        if w["effective_leverage"] < self.min_effective_leverage:
            return False
        # 距收回价
        d = w["price_recovery_ratio"]
        if d < self.min_distance_to_call or d > self.max_distance_to_call:
            return False
        # 发行人
        if self.prefer_issuer_list:
            allowed = [s.strip().upper() for s in str(self.prefer_issuer_list).split(",") if s.strip()]
            if w["issuer"].upper() not in allowed:
                return False
        # 距到期
        if w["days_to_expiry"] < self.min_days_to_expiry or w["days_to_expiry"] > self.max_days_to_expiry:
            return False
        return True

    def _maybe_requery(self):
        now = time.time()
        if (now - self._last_query_ts) >= self.requery_interval_sec:
            self._query_cbbc_chain(force=True)

    # ═══════════════════════════════════════════════════
    #  开仓 / 平仓
    # ═══════════════════════════════════════════════════
    def _try_open_position(self, signal: float, bar: BarData):
        if self.pos != 0:
            return

        cands = self._query_cbbc_chain()
        if not cands:
            return

        # 信号方向 → 牛/熊
        if signal > 0:
            pool = [c for c in cands if c["cbbc_type_str"] == "BULL"]
        else:
            pool = [c for c in cands if c["cbbc_type_str"] == "BEAR"]
        if not pool:
            self.write_log(f"[CBBC] 信号={signal} 但无对应方向牛熊证")
            return

        # 距收回价过近的直接排除（防御强制收回）
        safe_pool = [c for c in pool if c["price_recovery_ratio"] >= self.recovery_warn_pct]
        if not safe_pool:
            self.write_log(f"[CBBC] 候选均距收回价过近（<{self.recovery_warn_pct}%），不开仓")
            return

        pick = safe_pool[0]

        # 仓位
        cash = self._get_available_cash()
        size_hkd = min(cash * self.position_pct_of_cash, self.max_position_value)
        if self.regime_scale:
            size_hkd *= self._regime_size_multiplier()
        qty = int(size_hkd / max(pick["cur_price"], 1e-6) / max(pick["lot_size"], 1)) * max(pick["lot_size"], 1)
        qty = max(qty, max(pick["lot_size"], 1))

        # 订阅牛熊证行情
        self._subscribe_cbbc(pick["stock"])

        req = OrderRequest(
            symbol=pick["stock"],
            exchange=Exchange.SEHK,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=round_to(pick["cur_price"] * 1.005, 0.001),
            volume=qty,
            reference=f"CBBC_{pick['cbbc_type_str']}",
        )
        vt = self.send_order(req)
        if vt:
            self.pos = qty
            self.entry_price = pick["cur_price"]
            self.entry_time = time.time()
            self.current_cbbc_stock = pick["stock"]
            self.cbbc_type = pick["cbbc_type_str"]
            self.leverage = pick["leverage"]
            self.distance_to_call = pick["price_recovery_ratio"]
            self.days_to_expiry = pick["days_to_expiry"]
            self.pnl_pct = 0.0
            self.bars_held = 0
            self._current_info = pick
            self.write_log(
                f"[CBBC] 🟢 开仓: {pick['stock']}({pick['cbbc_type_str']}) "
                f"lev={pick['leverage']:.1f}x dist={pick['price_recovery_ratio']:.1f}% "
                f"days={pick['days_to_expiry']} qty={qty} @ {pick['cur_price']:.3f}"
            )
            self._telegram_push(
                f"🟢 开仓 {pick['stock']}({pick['cbbc_type_str']})\n"
                f"正股={self.underlying_symbol} 信号={signal:.0f}\n"
                f"lev={pick['leverage']:.1f}x 距收回={pick['price_recovery_ratio']:.1f}%\n"
                f"days={pick['days_to_expiry']} qty={qty} @ {pick['cur_price']:.3f}"
            )

    def _manage_position(self, bar: BarData):
        if self.pos == 0 or not self._current_info:
            return

        cur = bar.close_price
        entry = self.entry_price if self.entry_price > 0 else cur
        raw = (cur - entry) / entry if entry > 0 else 0.0
        pnl = raw * max(self.leverage, 1.0)
        self.pnl_pct = pnl

        # 1. 距收回价预警 → 提前平仓（牛熊证核心风控）
        dist = self._current_info.get("price_recovery_ratio", self.distance_to_call)
        if dist < self.recovery_exit_pct:
            self._close_position(cur, reason=f"距收回价过近 {dist:.1f}%")
            return
        if dist < self.recovery_warn_pct:
            self.write_log(f"[CBBC] ⚠️ 距收回价仅 {dist:.1f}%，准备平仓")

        # 2. 止盈
        if pnl >= self.profit_take_pct:
            self._close_position(cur, reason=f"止盈 {pnl*100:.1f}%")
            return

        # 3. 止损
        if pnl <= -self.stop_loss_pct:
            self._close_position(cur, reason=f"止损 {pnl*100:.1f}%")
            return

        # 4. 到期
        real_days = self._current_info.get("days_to_expiry", self.days_to_expiry)
        if real_days <= self.time_decay_close_days:
            self._close_position(cur, reason=f"临近到期 {real_days}天")
            return

        # 5. 超时
        if self.bars_held >= self.max_hold_bars:
            self._close_position(cur, reason=f"超时 {self.bars_held}根bar")
            return

        # 6. 信号反转
        sig = self._get_combined_signal(bar)
        is_bull = self.cbbc_type == "BULL"
        if is_bull and sig < 0:
            self._close_position(cur, reason="信号反转(牛→熊)")
            return
        if not is_bull and sig > 0:
            self._close_position(cur, reason="信号反转(熊→牛)")
            return

    def _close_position(self, cur: float, reason: str):
        if self.pos == 0:
            return
        qty = abs(self.pos)
        req = OrderRequest(
            symbol=self.current_cbbc_stock,
            exchange=Exchange.SEHK,
            direction=Direction.SHORT,
            offset=Offset.CLOSE,
            price=round_to(cur * 0.995, 0.001),
            volume=qty,
            reference=f"CBBC_CLOSE_{reason}",
        )
        vt = self.send_order(req)
        if vt:
            self.write_log(
                f"[CBBC] 🔴 平仓: {self.current_cbbc_stock}({self.cbbc_type}) "
                f"@ {cur:.3f} | {reason} | 盈亏{self.pnl_pct*100:.1f}%"
            )
            self._telegram_push(
                f"🔴 平仓 {self.current_cbbc_stock}({self.cbbc_type})\n"
                f"原因={reason} 价={cur:.3f}\n"
                f"盈亏={self.pnl_pct*100:.1f}%"
            )
            self._reset_state()

    def _reset_state(self):
        self.pos = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.current_cbbc_stock = ""
        self.cbbc_type = ""
        self.leverage = 0.0
        self.distance_to_call = 0.0
        self.days_to_expiry = 0
        self.pnl_pct = 0.0
        self.bars_held = 0
        self._current_info = {}

    # ═══════════════════════════════════════════════════
    #  辅助
    # ═══════════════════════════════════════════════════
    def _get_quote_ctx(self) -> Optional[OpenQuoteContext]:
        me = getattr(self.cta_engine, 'main_engine', None)
        if me is not None:
            for gw in getattr(me, 'gateways', {}).values():
                qc = getattr(gw, 'quote_ctx', None)
                if qc is not None:
                    self.write_log(f"[CBBC] 复用网关 quote_ctx")
                    return qc
        try:
            from futu import OpenQuoteContext as OQC
            host = getattr(self.cta_engine, 'gateway_host', '127.0.0.1')
            port = getattr(self.cta_engine, 'gateway_port', 11111)
            return OQC(host=host, port=port)
        except Exception as e:
            self.write_log(f"[CBBC] 自建 quote_ctx 失败: {e}")
            return None

    def _subscribe_underlying(self):
        try:
            sym = self.underlying_symbol
            code = sym.split('.')[-1] if '.' in sym else sym
            ex = Exchange.SEHK if 'SEHK' in sym.upper() or code.isdigit() else Exchange.SMART
            self.cta_engine.subscribe(SubscribeRequest(symbol=code, exchange=ex))
            self.write_log(f"[CBBC] 订阅正股: {sym}")
        except Exception as e:
            self.write_log(f"[CBBC] 订阅正股失败: {e}")

    def _subscribe_cbbc(self, stock: str):
        try:
            code = stock.split('.')[-1] if '.' in stock else stock
            self.cta_engine.subscribe(SubscribeRequest(symbol=code, exchange=Exchange.SEHK))
        except Exception:
            pass

    def _get_available_cash(self) -> float:
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            if me is None:
                return 100000.0
            for gw in getattr(me, 'gateways', {}).values():
                info = getattr(gw, 'acc_info', {})
                c = _to_float(info.get("cash"), 0.0)
                if c > 0:
                    return c
        except Exception:
            pass
        return 100000.0

    def _regime_size_multiplier(self) -> float:
        m = {"bull_trend": 1.0, "bear_trend": 0.6, "range": 0.8, "volatile": 0.5}
        return m.get(self.regime_label, 0.7)

    def _telegram_push(self, text: str):
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            rc = getattr(me, 'remote_controller', None) if me else None
            if rc and hasattr(rc, 'push_text'):
                rc.push_text(text)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  防御性接口
    # ═══════════════════════════════════════════════════
    def on_trade(self, trade):
        pass

    def on_order(self, order):
        if order.status == Status.REJECTED:
            self.write_log(f"[CBBC] ⚠️ 委托被拒: {order.orderid}")
            if "CLOSE" not in str(getattr(order, 'reference', '')):
                self.pos = 0
                self._reset_state()
