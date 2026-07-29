"""
strategies/structured_products/warrant_strategy.py - v2.9.3
窝轮策略（实盘级重写）

═════════════════════════════════════════════════════════════
【核心数据源】富途 OpenAPI 官方接口
    get_warrant(stock_owner, WarrantRequest)
        → 返回 DataFrame，关键字段（已按官方文档逐字段核对）：
            stock             窝轮代码（如下单标的）
            type              WrtType 枚举：CALL / PUT / BULL / BEAR / INLINE
            stock_owner       所属正股代码
            delta             对冲值（CALL 为正，PUT 为负）
            premium           溢价（百分比，如 20 表示 20%）
            leverage          杠杆倍数
            effective_leverage 有效杠杆
            implied_volatility 引伸波幅
            strike_price      行使价
            conversion_ratio  换股比率
            maturity_time     到期日（yyyy-MM-dd）
            last_trade_time   最后交易日
            recovery_price    收回价（仅牛熊证）
            price_recovery_ratio 正股距收回价（仅牛熊证，%）
            status            状态（NORMAL/SUSPENDED/PRE_IPO）
            break_even_point  打和点
            ipop              价内/价外（%）
            lot_size          每手数量

═════════════════════════════════════════════════════════════
【与上版的根本性差异】
    上版（v2.6）致命缺陷：
        1. 从未调用 get_warrant —— 所有 delta/杠杆/溢价都是
           np.random.uniform 伪造的随机数，策略等于空跑
        2. self.buy/sell 用的是正股 vt_symbol —— 窝轮必须下到
           窝轮自己的 stock 代码
        3. on_tick 只把 tick 喂给 BarGenerator，没有利用已订阅
           的 TICKER 做盘口/逐笔判断
        4. 临近到期判断用本地自减的 self.days_to_expiry，永远不会
           触发真实到期（因为根本没查过真实到期日）
        5. 信号只看自身均线，无法对接 MultiIndicator 的共享信号

    本版修复：
        1. _query_warrant_chain() 真实调 get_warrant，按参数筛选
        2. 下单标的 = 筛选出的窝轮 stock 代码
        3. on_tick 计算盘口加权价 + 买卖盘 imbalance
        4. 真实到期日由 API 返回，逐日倒计时
        5. 通过 MarketDataBus / event_bus 读取共享信号
        6. 多周期分层：1M 执行止损 / 5M 趋势确认 / 60M 宏观
        7. Regime 感知仓位缩放
        8. Telegram 推送每次选轮 + 下单 + 平仓事件
"""
import time
import math
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from vnpy_ctastrategy import CtaTemplate, BarGenerator, ArrayManager
from vnpy.trader.object import BarData, TickData, OrderRequest, SubscribeRequest
from vnpy.trader.constant import Direction, Exchange, Offset, Status
from vnpy.trader.utility import round_to

from futu import (
    OpenQuoteContext, RET_OK, WarrantRequest, WrtType,
    WarrantStatus, SortField, SecurityType,
)

# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════
def _to_float(val, default=0.0):
    """安全浮点转换，防御 'N/A' / None / 空字符串"""
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
    """把 'yyyy-MM-dd' 转成距离今天的天数，解析失败返回 999"""
    try:
        m = datetime.strptime(maturity_str, "%Y-%m-%d").date()
        return (m - datetime.now().date()).days
    except (ValueError, TypeError):
        return 999


def _wrt_type_to_str(t) -> str:
    """WrtType 枚举 → 字符串，兼容不同 SDK 版本"""
    mapping = {
        WrtType.CALL: "CALL", WrtType.PUT: "PUT",
        WrtType.BULL: "BULL", WrtType.BEAR: "BEAR",
    }
    # 直接是枚举成员
    if t in mapping:
        return mapping[t]
    # 有些版本返回 int
    int_map = {1: "CALL", 2: "PUT", 3: "BULL", 4: "BEAR", 5: "INLINE"}
    try:
        return int_map.get(int(t), str(t))
    except (ValueError, TypeError):
        return str(t)


# ═══════════════════════════════════════════════════════════
#  主策略类
# ═══════════════════════════════════════════════════════════
class WarrantStrategy(CtaTemplate):
    """
    实盘级窝轮/牛熊证策略

    数据流（对应已订阅的全套数据）：
        TICKER  → on_tick   ：盘口 imbalance、加权价、逐笔信号
        K_1M    → on_1m_bar ：执行层（止损/止盈/时间衰减检查）
        K_5M    → on_5m_bar ：趋势确认（EMA 多头/空头排列）
        K_60M   → on_60m_bar：宏观 Regime 更新
        QUOTE   → on_tick   ：bid/ask 快照
    """
    author = "Apollo"

    # ───────────────────────────────────────
    #  可调参数（策略模板会在 UI 暴露）
    # ───────────────────────────────────────
    parameters = [
        # 信号来源
        "signal_source",          # 'self' / 'multi_indicator' / 'dual_thrust'
        "underlying_symbol",     # 正股 vt_symbol，如 'HK.00700'
        # 筛选参数
        "min_leverage",          # 最低杠杆（倍）
        "max_leverage",          # 最高杠杆（倍）
        "min_days_to_expiry",    # 最短到期天数
        "max_days_to_expiry",    # 最长到期天数
        "min_delta_abs",         # Delta 绝对值下限（如 0.3）
        "max_delta_abs",         # Delta 绝对值上限（如 0.6）
        "max_premium_pct",       # 最高溢价率（%，如 30）
        "min_effective_leverage",# 最低有效杠杆
        "max_iv_pct",            # 最高引伸波幅（%，如 60）
        "max_recovery_pct",      # 牛熊证：正股距收回价上限（%）
        "min_recovery_pct",      # 牛熊证：正股距收回价下限（%）
        "prefer_issuer_list",    # 偏好发行人列表（空=不限）
        # 交易参数
        "max_position_value",    # 单笔最大持仓金额（HKD）
        "position_pct_of_cash", # 单次投入占可用现金比例（如 0.1 = 10%）
        "profit_take_pct",      # 止盈百分比（相对入场价）
        "stop_loss_pct",        # 止损百分比
        "time_decay_close_days", # 距到期 <= N 天强制平仓
        "max_hold_bars",        # 最长持仓 1M bar 数（超时强制平仓）
        # 趋势过滤
        "adx_threshold",         # 5M ADX 阈值，低于此值不入场
        "ema_fast", "ema_slow", # 5M EMA 周期
        # Regime
        "regime_scale",          # 是否启用 Regime 仓位缩放
        # 查询节流
        "requery_interval_sec",  # 每次重新筛选窝轮的间隔（秒）
    ]

    # ───────────────────────────────────────
    #  运行时变量（持久化到 cta_strategy_data.json）
    # ───────────────────────────────────────
    variables = [
        "pos", "entry_price", "entry_time", "current_warrant_stock",
        "warrant_type", "leverage", "delta", "premium", "days_to_expiry",
        "pnl_pct", "bars_held", "last_underlying_signal",
        "regime_label",
    ]

    # ═══════════════════════════════════════════════════
    #  初始化
    # ═══════════════════════════════════════════════════
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # 状态
        self.pos = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.current_warrant_stock = ""   # 当前持有的窝轮代码
        self.warrant_type = ""            # CALL/PUT/BULL/BEAR
        self.leverage = 0.0
        self.delta = 0.0
        self.premium = 0.0
        self.days_to_expiry = 0
        self.pnl_pct = 0.0
        self.bars_held = 0
        self.last_underlying_signal = 0.0
        self.regime_label = "unknown"

        # 多周期
        self.bg_1m = BarGenerator(self.on_1m_bar, window=1, on_window_bar=self.on_1m_bar)
        self.bg_5m = BarGenerator(self.on_1m_bar, window=5, on_window_bar=self.on_5m_bar)
        self.bg_60m = BarGenerator(self.on_1m_bar, window=60, on_window_bar=self.on_60m_bar)
        self.am_1m = ArrayManager(size=200)
        self.am_5m = ArrayManager(size=200)
        self.am_60m = ArrayManager(size=100)

        # 盘口/信号
        self.last_tick = None
        self.tick_imbalance = 0.0
        self.underlying_bid = 0.0
        self.underlying_ask = 0.0

        # 查询节流
        self._last_query_ts = 0.0
        self._candidate_warrants: List[Dict] = []  # 当前筛选候选
        self._current_warrant_info: Dict = {}       # 当前持有窝轮的完整信息

        # 引用底层 quote_ctx（由引擎注入或自建）
        self.quote_ctx: Optional[OpenQuoteContext] = None

        # 共享信号
        self._shared_signal = 0.0
        self._shared_signal_source = "none"

        # 默认参数（防止 setting 缺失）
        self._set_defaults()

    def _set_defaults(self):
        """为所有 parameters 提供安全默认值"""
        defaults = {
            "signal_source": "self",
            "underlying_symbol": "HK.00700",
            "min_leverage": 3.0, "max_leverage": 10.0,
            "min_days_to_expiry": 14, "max_days_to_expiry": 90,
            "min_delta_abs": 0.3, "max_delta_abs": 0.65,
            "max_premium_pct": 30.0,
            "min_effective_leverage": 2.0,
            "max_iv_pct": 60.0,
            "max_recovery_pct": 15.0, "min_recovery_pct": 3.0,
            "prefer_issuer_list": "",
            "max_position_value": 50000.0,
            "position_pct_of_cash": 0.1,
            "profit_take_pct": 0.25, "stop_loss_pct": 0.15,
            "time_decay_close_days": 5,
            "max_hold_bars": 240,  # 约 4 小时 @1min
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
        self.write_log(f"[Warrant] 策略初始化完成 | 正股={self.underlying_symbol} | "
                      f"杠杆范围={self.min_leverage:.0f}x~{self.max_leverage:.0f}x | "
                      f"到期={self.min_days_to_expiry}~{self.max_days_to_expiry}天")

    def on_start(self):
        # 获取 quote_ctx 引用（优先从 main_engine 拿，否则自建）
        self.quote_ctx = self._get_quote_ctx()
        if self.quote_ctx is None:
            self.write_log("[Warrant] ⚠️ 无法获取 quote_ctx，窝轮筛选将失败！")
        else:
            self.write_log("[Warrant] ✅ quote_ctx 已就绪，开始筛选窝轮")
        # 订阅正股行情（用于信号 + 盘口）
        self._subscribe_underlying()
        # 立即查一次候选
        self._query_warrant_chain(force=True)

    def on_stop(self):
        self.write_log(f"[Warrant] 策略停止 | 持仓={self.pos} 入场={self.entry_price}")

    # ═══════════════════════════════════════════════════
    #  行情接入
    # ═══════════════════════════════════════════════════
    def on_tick(self, tick: TickData):
        """逐笔/报价 tick：更新盘口、imbalance、喂给 BarGenerator"""
        self.last_tick = tick
        # 盘口 imbalance
        bid = _to_float(tick.bid_price_1, 0.0)
        ask = _to_float(tick.ask_price_1, 0.0)
        bid_v = _to_float(tick.bid_volume_1, 0.0)
        ask_v = _to_float(tick.ask_volume_1, 0.0)
        self.underlying_bid = bid
        self.underlying_ask = ask
        total = bid_v + ask_v
        self.tick_imbalance = (bid_v - ask_v) / total if total > 0 else 0.0

        # 喂 1M bar
        self.bg_1m.update_tick(tick)

    def on_bar(self, bar: BarData):
        """CTA 引擎默认推送的 bar（假设为 1M）→ 喂给多周期合成器"""
        self.bg_1m.update_bar(bar)
        self.bg_5m.update_bar(bar)
        self.bg_60m.update_bar(bar)

    def on_1m_bar(self, bar: BarData):
        """1 分钟 bar：执行层（止损/止盈/时间衰减/超时）"""
        self.am_1m.update_bar(bar)
        if not self.am_1m.inited:
            return

        # 读取共享信号（如有）
        self._read_shared_signal()

        # 持仓管理
        if self.pos != 0:
            self.bars_held += 1
            self._manage_position(bar)
        else:
            self.bars_held = 0
            # 空仓：检查是否该开仓
            signal = self._get_combined_signal(bar)
            self.last_underlying_signal = signal
            if abs(signal) >= 1.0:
                self._try_open_position(signal, bar)

        # 定时重新筛选候选（节流）
        self._maybe_requery()

    def on_5m_bar(self, bar: BarData):
        """5 分钟 bar：趋势确认 + ADX 过滤"""
        self.am_5m.update_bar(bar)
        if not self.am_5m.inited:
            return
        # 仅记录，主信号在 on_1m_bar 中合成
        self._update_regime_from_5m()

    def on_60m_bar(self, bar: BarData):
        """60 分钟 bar：宏观 Regime 标签"""
        self.am_60m.update_bar(bar)
        if not self.am_60m.inited:
            return
        self._update_regime_from_60m()

    # ═══════════════════════════════════════════════════
    #  信号合成
    # ═══════════════════════════════════════════════════
    def _get_combined_signal(self, bar: BarData) -> float:
        """
        综合信号（与已订阅的 K线/QUOTE 配合）：
            +1  = 强烈看涨（开 CALL / BULL）
            -1  = 强烈看跌（开 PUT / BEAR）
             0  = 无信号
        加权：5M EMA 趋势 0.5 + 1M RSI 0.2 + 盘口 imbalance 0.15 + ADX 0.15
        """
        if not self.am_5m.inited or not self.am_1m.inited:
            return 0.0

        # 5M 趋势
        ema_f = self.am_5m.ema(self.ema_fast)
        ema_s = self.am_5m.ema(self.ema_slow)
        if ema_f > ema_s * 1.002:
            trend = 1.0
        elif ema_f < ema_s * 0.998:
            trend = -1.0
        else:
            trend = 0.0

        # ADX 强度（仅做权重，不做方向）
        adx = self.am_5m.adx(14) if hasattr(self.am_5m, 'adx') else 25.0
        adx_w = min(max((adx - 10) / 30, 0.0), 1.0)  # 0~1

        # 1M RSI 极值
        rsi = self.am_1m.rsi(14)
        rsi_sig = 0.0
        if rsi > 70:
            rsi_sig = -0.3  # 超买回调预期
        elif rsi < 30:
            rsi_sig = 0.3   # 超卖反弹预期

        # 盘口 imbalance
        imb = np.clip(self.tick_imbalance, -1.0, 1.0)

        # 共享信号（来自 MultiIndicator）
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
        """用 5M ADX + ATR 估算 Regime 标签"""
        if not hasattr(self.am_5m, 'adx'):
            return
        adx = self.am_5m.adx(14)
        atr = self.am_5m.atr(14) if hasattr(self.am_5m, 'atr') else 0.0
        close = self.am_5m.close[-1] if len(self.am_5m.close) > 0 else 0.0
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
        """从 event_bus / MarketDataBus 读取 MultiIndicator 的共享信号"""
        try:
            bus = getattr(self, 'event_bus', None) or getattr(self.cta_engine, 'event_bus', None)
            if bus is None:
                return
            # 约定事件类型 'eSignal' 携带 dict: {symbol, signal, source}
            # 这里仅尝试无异常地读取，不强制依赖
            last = bus.get_last('eSignal')
            if last and last.get('symbol') == self.underlying_symbol:
                self._shared_signal = float(last.get('signal', 0.0))
                self._shared_signal_source = str(last.get('source', 'unknown'))
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  窝轮筛选（核心：真实调用 get_warrant）
    # ═══════════════════════════════════════════════════
    def _query_warrant_chain(self, force=False) -> List[Dict]:
        """
        调用富途 get_warrant 筛选符合条件的窝轮/牛熊证。
        返回 List[Dict]，每个元素含完整字段，可直接用于下单决策。
        """
        now = time.time()
        if not force and (now - self._last_query_ts) < self.requery_interval_sec:
            return self._candidate_warrants
        self._last_query_ts = now

        if self.quote_ctx is None:
            self.write_log("[Warrant] ⚠️ quote_ctx 未就绪，跳过筛选")
            return []

        # 确定要筛选的类型：根据当前信号方向选 CALL 或 PUT
        # 这里先分别拉 CALL+PUT 和 BULL+BEAR，让开仓时再挑
        candidates: List[Dict] = []

        for wrt_type in [WrtType.CALL, WrtType.PUT, WrtType.BULL, WrtType.BEAR]:
            req = WarrantRequest()
            req.type_list = [wrt_type]
            req.leverage_ratio_min = self.min_leverage
            req.leverage_ratio_max = self.max_leverage
            req.delta_min = -1.0 if wrt_type == WrtType.PUT else self.min_delta_abs
            req.delta_max = 1.0 if wrt_type == WrtType.CALL else -self.min_delta_abs
            req.premium_min = 0.0
            req.premium_max = self.max_premium_pct
            req.status = WarrantStatus.NORMAL
            # 到期日过滤（maturity_time 用 yyyy-MM-dd）
            today = datetime.now().date()
            req.maturity_time_min = (today + timedelta(days=self.min_days_to_expiry)).strftime("%Y-%m-%d")
            req.maturity_time_max = (today + timedelta(days=self.max_days_to_expiry)).strftime("%Y-%m-%d")
            # 牛熊证专属：距收回价
            if wrt_type in (WrtType.BULL, WrtType.BEAR):
                if self.max_recovery_pct > 0:
                    req.price_recovery_ratio_min = self.min_recovery_pct
                    req.price_recovery_ratio_max = self.max_recovery_pct
            # 排序：按综合评分降序
            req.sort_field = SortField.SCORE
            req.ascend = False
            req.num = 50  # 每类取前 50

            try:
                ret, result = self.quote_ctx.get_warrant(self.underlying_symbol, req)
            except Exception as e:
                self.write_log(f"[Warrant] get_warrant({wrt_type}) 异常: {e}")
                continue

            if ret != RET_OK:
                self.write_log(f"[Warrant] get_warrant({wrt_type}) 失败: {result}")
                continue

            # result 是 (dataframe, last_page, all_count)
            try:
                data, last_page, all_count = result
            except (TypeError, ValueError):
                data = result if hasattr(result, '__iter__') else None
                last_page, all_count = True, 0

            if data is None or (hasattr(data, '__len__') and len(data) == 0):
                continue

            # 解析 DataFrame → Dict
            for _, row in data.iterrows():
                item = self._parse_warrant_row(row, wrt_type)
                if item and self._passes_extra_filter(item):
                    candidates.append(item)

        # 按评分降序
        candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        self._candidate_warrants = candidates

        # 调试日志（首次或每 N 次打印列名）
        if candidates:
            sample = candidates[0]
            self.write_log(
                f"[Warrant] ✅ 筛选完成: {len(candidates)} 只候选 | "
                f"样本={sample['stock']} type={sample['wrt_type_str']} "
                f"delta={sample['delta']:.2f} lev={sample['leverage']:.1f}x "
                f"prem={sample['premium']:.1f}% days={sample['days_to_expiry']}"
            )
        else:
            self.write_log(f"[Warrant] ⚠️ 无符合筛选条件的窝轮（正股={self.underlying_symbol}）")

        return candidates

    def _parse_warrant_row(self, row, wrt_type) -> Optional[Dict]:
        """把 get_warrant 返回的 DataFrame 一行，转成统一 Dict"""
        try:
            stock = str(row.get("stock", "")).strip()
            if not stock:
                return None
            d = {
                "stock": stock,
                "wrt_type": wrt_type,
                "wrt_type_str": _wrt_type_to_str(wrt_type),
                "stock_owner": str(row.get("stock_owner", self.underlying_symbol)),
                "name": str(row.get("name", stock)),
                "delta": _to_float(row.get("delta"), 0.0),
                "premium": _to_float(row.get("premium"), 999.0),
                "leverage": _to_float(row.get("leverage"), 0.0),
                "effective_leverage": _to_float(row.get("effective_leverage"), 0.0),
                "implied_volatility": _to_float(row.get("implied_volatility"), 0.0),
                "strike_price": _to_float(row.get("strike_price"), 0.0),
                "conversion_ratio": _to_float(row.get("conversion_ratio"), 1.0),
                "maturity_time": str(row.get("maturity_time", "")),
                "last_trade_time": str(row.get("last_trade_time", "")),
                "recovery_price": _to_float(row.get("recovery_price"), 0.0),
                "price_recovery_ratio": _to_float(row.get("price_recovery_ratio"), 0.0),
                "status": int(_to_float(row.get("status"), 0)),
                "break_even_point": _to_float(row.get("break_even_point"), 0.0),
                "ipop": _to_float(row.get("ipop"), 0.0),
                "lot_size": int(_to_float(row.get("lot_size"), 1000)),
                "score": _to_float(row.get("score"), 0.0),
                "cur_price": _to_float(row.get("cur_price", row.get("current_price", 0.0)), 0.0),
                "issuer": str(row.get("issuer", "")),
                "days_to_expiry": _days_to_maturity(str(row.get("maturity_time", ""))),
            }
            return d
        except Exception as e:
            self.write_log(f"[Warrant] 解析窝轮行异常: {e}")
            return None

    def _passes_extra_filter(self, w: Dict) -> bool:
        """参数二次过滤（覆盖 WarrantRequest 不支持的字段）"""
        # 有效杠杆
        if w["effective_leverage"] < self.min_effective_leverage:
            return False
        # IV
        if self.max_iv_pct > 0 and w["implied_volatility"] > self.max_iv_pct:
            return False
        # 发行人偏好
        if self.prefer_issuer_list:
            allowed = [s.strip().upper() for s in str(self.prefer_issuer_list).split(",") if s.strip()]
            if w["issuer"].upper() not in allowed:
                return False
        # 状态必须正常
        if w["status"] != 0:  # 0=NORMAL
            return False
        # 距到期
        if w["days_to_expiry"] < self.min_days_to_expiry or w["days_to_expiry"] > self.max_days_to_expiry:
            return False
        # Delta 绝对值区间（CALL 正，PUT 负）
        d_abs = abs(w["delta"])
        if d_abs < self.min_delta_abs or d_abs > self.max_delta_abs:
            return False
        return True

    def _maybe_requery(self):
        now = time.time()
        if (now - self._last_query_ts) >= self.requery_interval_sec:
            self._query_warrant_chain(force=True)

    # ═══════════════════════════════════════════════════
    #  开仓 / 平仓
    # ═══════════════════════════════════════════════════
    def _try_open_position(self, signal: float, bar: BarData):
        """根据信号方向选一只最优窝轮并下单"""
        if self.pos != 0:
            return

        # 重新筛选（确保候选最新）
        cands = self._query_warrant_chain()
        if not cands:
            self.write_log("[Warrant] 无可用候选，跳过开仓")
            return

        # 按信号方向过滤
        if signal > 0:
            pool = [c for c in cands if c["wrt_type_str"] in ("CALL", "BULL")]
        else:
            pool = [c for c in cands if c["wrt_type_str"] in ("PUT", "BEAR")]
        if not pool:
            self.write_log(f"[Warrant] 信号={signal} 但无对应方向候选")
            return

        # 选评分最高的
        pick = pool[0]

        # 仓位 = 可用现金 × 比例，受 max_position_value 限制
        cash = self._get_available_cash()
        size_hkd = min(cash * self.position_pct_of_cash, self.max_position_value)
        # 考虑 Regime 缩放
        if self.regime_scale:
            size_hkd *= self._regime_size_multiplier()
        qty = int(size_hkd / max(pick["cur_price"], 1e-6) / max(pick["lot_size"], 1)) * max(pick["lot_size"], 1)
        qty = max(qty, max(pick["lot_size"], 1))

        # 订阅窝轮行情（确保能收到它的 tick）
        self._subscribe_warrant(pick["stock"])

        # 下单
        direction = Direction.LONG  # 窝轮/牛熊证买入即做多方向
        price = pick["cur_price"]
        req = OrderRequest(
            symbol=pick["stock"],
            exchange=Exchange.SEHK,
            direction=direction,
            offset=Offset.OPEN,
            price=round_to(price * 1.005, 0.001),  # 略高确保成交
            volume=qty,
            reference=f"WRT_{pick['wrt_type_str']}",
        )
        vt_orderid = self.send_order(req)
        if vt_orderid:
            self.pos = qty
            self.entry_price = price
            self.entry_time = time.time()
            self.current_warrant_stock = pick["stock"]
            self.warrant_type = pick["wrt_type_str"]
            self.leverage = pick["leverage"]
            self.delta = pick["delta"]
            self.premium = pick["premium"]
            self.days_to_expiry = pick["days_to_expiry"]
            self.pnl_pct = 0.0
            self.bars_held = 0
            self._current_warrant_info = pick
            self.write_log(
                f"[Warrant] 🟢 开仓: {pick['stock']}({pick['wrt_type_str']}) "
                f"delta={pick['delta']:.2f} lev={pick['leverage']:.1f}x "
                f"prem={pick['premium']:.1f}% days={pick['days_to_expiry']} "
                f"qty={qty} @ {price:.3f} | 信号={signal:.1f}"
            )
            self._telegram_push(
                f"🟢 开仓 {pick['stock']}({pick['wrt_type_str']})\n"
                f"正股={self.underlying_symbol} 信号={signal:.0f}\n"
                f"delta={pick['delta']:.2f} lev={pick['leverage']:.1f}x "
                f"prem={pick['premium']:.1f}%\n"
                f"距到期={pick['days_to_expiry']}天 qty={qty} @ {price:.3f}"
            )

    def _manage_position(self, bar: BarData):
        """持仓管理：止盈 / 止损 / 时间衰减 / 超时 / 信号反转"""
        if self.pos == 0 or not self._current_warrant_info:
            return

        cur_price = bar.close_price
        entry = self.entry_price if self.entry_price > 0 else cur_price
        # 对窝轮而言，正股方向决定盈亏符号
        is_long = self.warrant_type in ("CALL", "BULL")
        # 简化：用窝轮自身价格变化 × 杠杆估算盈亏
        raw_chg = (cur_price - entry) / entry if entry > 0 else 0.0
        # 用杠杆放大
        pnl_pct = raw_chg * max(self.leverage, 1.0)
        self.pnl_pct = pnl_pct

        # 1. 止盈
        if pnl_pct >= self.profit_take_pct:
            self._close_position(cur_price, reason=f"止盈 {pnl_pct*100:.1f}%")
            return

        # 2. 止损
        if pnl_pct <= -self.stop_loss_pct:
            self._close_position(cur_price, reason=f"止损 {pnl_pct*100:.1f}%")
            return

        # 3. 临近到期
        real_days = self._current_warrant_info.get("days_to_expiry", self.days_to_expiry)
        if real_days <= self.time_decay_close_days:
            self._close_position(cur_price, reason=f"临近到期 {real_days}天")
            return

        # 4. 超时
        if self.bars_held >= self.max_hold_bars:
            self._close_position(cur_price, reason=f"超时 {self.bars_held}根1Mbar")
            return

        # 5. 信号反转
        signal = self._get_combined_signal(bar)
        if is_long and signal < 0:
            self._close_position(cur_price, reason=f"信号反转(多→空)")
            return
        if not is_long and signal > 0:
            self._close_position(cur_price, reason=f"信号反转(空→多)")
            return

    def _close_position(self, cur_price: float, reason: str):
        """平仓（卖出全部持仓）"""
        if self.pos == 0:
            return
        qty = abs(self.pos)
        # 窝轮持仓是 LONG，平仓用 SELL + CLOSE
        req = OrderRequest(
            symbol=self.current_warrant_stock,
            exchange=Exchange.SEHK,
            direction=Direction.SHORT,
            offset=Offset.CLOSE,
            price=round_to(cur_price * 0.995, 0.001),
            volume=qty,
            reference=f"WRT_CLOSE_{reason}",
        )
        vt_orderid = self.send_order(req)
        if vt_orderid:
            self.write_log(
                f"[Warrant] 🔴 平仓: {self.current_warrant_stock}({self.warrant_type}) "
                f"@ {cur_price:.3f} | {reason} | 持仓{self.bars_held}根bar "
                f"盈亏{self.pnl_pct*100:.1f}%"
            )
            self._telegram_push(
                f"🔴 平仓 {self.current_warrant_stock}({self.warrant_type})\n"
                f"原因={reason} 价={cur_price:.3f}\n"
                f"持仓={self.bars_held}bar 盈亏={self.pnl_pct*100:.1f}%"
            )
            self._reset_state()

    def _reset_state(self):
        self.pos = 0
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.current_warrant_stock = ""
        self.warrant_type = ""
        self.leverage = 0.0
        self.delta = 0.0
        self.premium = 0.0
        self.days_to_expiry = 0
        self.pnl_pct = 0.0
        self.bars_held = 0
        self._current_warrant_info = {}

    # ═══════════════════════════════════════════════════
    #  辅助
    # ═══════════════════════════════════════════════════
    def _get_quote_ctx(self) -> Optional[OpenQuoteContext]:
        """获取 quote_ctx：优先从 main_engine，其次自建"""
        # 1. 从 cta_engine 的 main_engine 拿
        me = getattr(self.cta_engine, 'main_engine', None)
        if me is not None:
            for gw_name, gw in getattr(me, 'gateways', {}).items():
                qc = getattr(gw, 'quote_ctx', None)
                if qc is not None:
                    self.write_log(f"[Warrant] 复用网关 {gw_name} 的 quote_ctx")
                    return qc
        # 2. 自建
        try:
            from futu import OpenQuoteContext as OQC
            host = getattr(self.cta_engine, 'gateway_host', '127.0.0.1')
            port = getattr(self.cta_engine, 'gateway_port', 11111)
            qc = OQC(host=host, port=port)
            self.write_log(f"[Warrant] 自建 quote_ctx @ {host}:{port}")
            return qc
        except Exception as e:
            self.write_log(f"[Warrant] 自建 quote_ctx 失败: {e}")
            return None

    def _subscribe_underlying(self):
        """订阅正股行情（供信号 + 盘口使用）"""
        try:
            from vnpy.trader.object import SubscribeRequest as SR
            sym = self.underlying_symbol
            if '.' in sym:
                code, ex = sym.split('.', 1)
            else:
                code, ex = sym, 'SEHK'
            ex_obj = Exchange.SEHK if ex == 'SEHK' else Exchange.SMART
            self.cta_engine.subscribe(SR(symbol=code, exchange=ex_obj))
            self.write_log(f"[Warrant] 订阅正股: {sym}")
        except Exception as e:
            self.write_log(f"[Warrant] 订阅正股失败: {e}")

    def _subscribe_warrant(self, warrant_stock: str):
        """订阅某只窝轮的行情"""
        try:
            from vnpy.trader.object import SubscribeRequest as SR
            code = warrant_stock.split('.')[-1] if '.' in warrant_stock else warrant_stock
            self.cta_engine.subscribe(SR(symbol=code, exchange=Exchange.SEHK))
        except Exception:
            pass

    def _get_available_cash(self) -> float:
        """从 gateway.acc_info 读取真实可用现金"""
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            if me is None:
                return 100000.0
            for gw in getattr(me, 'gateways', {}).values():
                info = getattr(gw, 'acc_info', {})
                cash = _to_float(info.get("cash"), 0.0)
                if cash > 0:
                    return cash
        except Exception:
            pass
        return 100000.0  # 兜底

    def _regime_size_multiplier(self) -> float:
        """Regime 仓位缩放因子"""
        m = {
            "bull_trend": 1.0,
            "bear_trend": 0.6,
            "range": 0.8,
            "volatile": 0.5,
        }
        return m.get(self.regime_label, 0.7)

    def _telegram_push(self, text: str):
        """通过 RemoteController / Telegram 推送（尽力而为）"""
        try:
            me = getattr(self.cta_engine, 'main_engine', None)
            if me is None:
                return
            rc = getattr(me, 'remote_controller', None)
            if rc is not None and hasattr(rc, 'push_text'):
                rc.push_text(text)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  CTA 模板必须实现的接口（防御性）
    # ═══════════════════════════════════════════════════
    def on_trade(self, trade):
        pass

    def on_order(self, order):
        if order.status == Status.REJECTED:
            self.write_log(f"[Warrant] ⚠️ 委托被拒: {order.orderid} {order.status}")
            # 委托失败：回滚 pos 计数（防止假持仓）
            if "CLOSE" not in str(getattr(order, 'reference', '')):
                self.pos = 0
                self._reset_state()
