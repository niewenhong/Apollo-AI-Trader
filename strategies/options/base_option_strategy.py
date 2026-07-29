"""
strategies/options/base_option_strategy.py - Apollo-AI-Trader v2.9.3
期权策略基类 v2：封装期权通用逻辑（查询链、批量报价、筛选、展期、平仓）

⚠️ 富途 API 字段权威来源（openapi.futunn.com）：
  get_option_expiration_date → strike_time, option_expiry_date_distance, expiration_cycle
  get_option_chain_by_date   → code, name, lot_size, option_type, stock_owner,
                                 strike_time, strike_price, suspension, ...
                                 ❗ 不含 delta/iv/premium，这些在 get_option_quote 里
  get_option_quote(legs)     → price, premium, implied_volatility, delta, gamma,
                                 vega, theta, rho, option_type("CALL"/"PUT"),
                                 expire_time, strike_price, contract_size,
                                 contract_multiplier, days_to_expiry,
                                 intrinsic_value, time_value, breakeven_point,
                                 dist_to_breakeven, prob_of_profit, seller_roi,
                                 mark_price, ...
"""
from vnpy_ctastrategy import CtaTemplate
from vnpy.trader.object import BarData, TickData, OrderData, TradeData
from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
from vnpy.trader.utility import BarGenerator
import time
from datetime import datetime
from typing import Optional, Dict, List, Tuple

# 富途 OptionType 枚举值（字符串，来自 get_option_quote / get_option_chain）
OPTION_TYPE_CALL = "CALL"
OPTION_TYPE_PUT  = "PUT"


class BaseOptionStrategy(CtaTemplate):
    """期权策略基类 v2"""

    # ── 通用参数（子类可覆盖） ──────────────────────────────
    min_days_to_expiry   = 7
    max_days_to_expiry   = 45
    min_otm_prob         = 0.60    # 对应 get_option_quote 的 prob_of_profit
    min_annual_roi       = 0.30
    max_positions        = 5
    position_size        = 1
    roll_when_ditm       = 0.30    # delta 绝对值超过此值展期
    cash_buffer_ratio    = 0.10

    # ── 多周期 ─────────────────────────────────────────────
    bar_frequencies = ["1m", "5m", "60m"]

    # ── 变量（写入状态文件） ────────────────────────────────
    variables = ["net_premium", "max_loss", "max_profit", "pnl",
                 "legs", "regime_label", "last_quote_ts"]

    # ──────────────────────────────────────────────────────
    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.net_premium   = 0.0
        self.max_loss      = 0.0
        self.max_profit    = 0.0
        self.pnl           = 0.0
        self.legs: Dict[str, dict] = {}
        self.regime_label   = "unknown"
        self.last_quote_ts  = 0.0

        self.bg = BarGenerator(self.on_bar, 1, self.on_1m_bar)
        self.bg5  = BarGenerator(self.on_bar, 5,  self.on_5m_bar)
        self.bg60 = BarGenerator(self.on_bar, 60, self.on_60m_bar)

        self._quote_cache: Dict[str, dict] = {}   # code → 报价 dict
        self._quote_cache_ts = 0.0
        self._pending_fill = False
        self._retry_count: Dict[str, int] = {}

    # ── 生命周期 ──────────────────────────────────────────
    def on_init(self):
        self.write_log(f"[{self.__class__.__name__}] on_init | {self.vt_symbol}")

    def on_start(self):
        self.write_log(f"[{self.__class__.__name__}] on_start | {self.vt_symbol}")

    def on_stop(self):
        self.write_log(f"[{self.__class__.__name__}] on_stop | {self.vt_symbol}")

    # ── 行情入口 ──────────────────────────────────────────
    def on_tick(self, tick: TickData):
        self.bg.update_tick(tick)
        # 快速展期检测（无需等bar）
        if self.legs:
            for name, leg in list(self.legs.items()):
                d = abs(leg.get("delta", 0))
                if d > self.roll_when_ditm:
                    self.write_log(f"[Base] tick触发展期 {name} delta={d:.2f}")
                    self._roll_positions()
                    return

    def on_bar(self, bar: BarData):
        self.bg5.update_bar(bar)
        self.bg60.update_bar(bar)
        self.on_1m_bar(bar)  # 子类可覆盖

    def on_1m_bar(self, bar: BarData):
        pass

    def on_5m_bar(self, bar: BarData):
        pass

    def on_60m_bar(self, bar: BarData):
        pass

    # ── 订单/成交 ────────────────────────────────────────
    def on_order(self, order: OrderData):
        pass

    def on_trade(self, trade: TradeData):
        self.write_log(f"[Base] 成交 {trade.symbol} {trade.direction.value} "
                       f"{trade.volume}@{trade.price}")

    # ──────────────────────────────────────────────────────
    #  富途 API 封装（字段已按官方文档校正）
    # ──────────────────────────────────────────────────────

    def _get_gateway(self):
        for name in ("FUTU_US", "FUTU_HK", "FUTU"):
            gw = self.cta_engine.main_engine.get_gateway(name)
            if gw and hasattr(gw, "quote_ctx"):
                return gw
        return None

    def _to_futu_code(self) -> str:
        if ".SMART" in self.vt_symbol:
            return f"US.{self.vt_symbol.split('.')[0]}"
        if ".SEHK" in self.vt_symbol:
            return f"HK.{self.vt_symbol.split('.')[0]}"
        return self.vt_symbol

    def _query_expiry_dates(self, code: str) -> List[dict]:
        """获取到期日列表。返回 [{"strike_time":"2026-08-15","distance":17}, ...]"""
        gw = self._get_gateway()
        if not gw:
            return []
        try:
            ret, data = gw.quote_ctx.get_option_expiration_date(code)
            if ret != 0 or data is None or data.empty:
                self.write_log(f"[Base] get_option_expiration_date 失败: {data}")
                return []
            out = []
            for _, r in data.iterrows():
                out.append({
                    "strike_time": str(r.get("strike_time", "")),
                    "distance":   int(r.get("option_expiry_date_distance", 999)),
                })
            return out
        except Exception as e:
            self.write_log(f"[Base] 查询到期日异常: {e}")
            return []

    def _query_chain_by_date(self, code: str, expiry_date: str) -> List[dict]:
        """获取指定到期日的所有合约（仅基础字段，无希腊值）。

        返回字段：code, name, option_type("CALL"/"PUT"), strike_price,
                 strike_time, lot_size, stock_owner, suspension, ...
        """
        gw = self._get_gateway()
        if not gw:
            return []
        try:
            ret, data = gw.quote_ctx.get_option_chain_by_date(code, expiry_date, expiry_date)
            if ret != 0 or data is None or data.empty:
                self.write_log(f"[Base] get_option_chain_by_date({expiry_date}) 失败: {data}")
                return []
            out = []
            for _, r in data.iterrows():
                ot = str(r.get("option_type", "")).upper()
                if ot not in (OPTION_TYPE_CALL, OPTION_TYPE_PUT):
                    continue
                out.append({
                    "code":         str(r.get("code", "")),
                    "name":         str(r.get("name", "")),
                    "option_type":  ot,                 # "CALL" / "PUT"
                    "is_call":      (ot == OPTION_TYPE_CALL),
                    "is_put":       (ot == OPTION_TYPE_PUT),
                    "strike_price": float(r.get("strike_price", 0) or 0),
                    "strike_time":  str(r.get("strike_time", "")),
                    "lot_size":     int(r.get("lot_size", 100) or 100),
                    "suspension":   bool(r.get("suspension", False)),
                })
            return out
        except Exception as e:
            self.write_log(f"[Base] 查询期权链异常: {e}")
            return []

    def _batch_quote(self, codes: List[str]) -> Dict[str, dict]:
        """批量获取期权报价（含 delta/iv/premium/days_to_expiry 等）。

        返回 {code: {price, premium, implied_volatility, delta, gamma,
                     vega, theta, rho, option_type, expire_time, strike_price,
                     contract_size, contract_multiplier, days_to_expiry,
                     intrinsic_value, time_value, breakeven_point,
                     prob_of_profit, seller_roi, mark_price, ...}}
        """
        gw = self._get_gateway()
        result: Dict[str, dict] = {}
        if not gw or not codes:
            return result
        try:
            from futu import OptionStrategyLeg, OptionStrategyType
            legs = []
            for c in codes:
                legs.append(OptionStrategyLeg(code=c, action="BUY", quantity=1.0))
            ret, data = gw.quote_ctx.get_option_quote(legs)
            if ret != 0 or data is None or data.empty:
                self.write_log(f"[Base] get_option_quote 失败: {data}")
                return result
            for _, r in data.iterrows():
                code = str(r.get("option_type" ""))  # placeholder, see below
                # 用 index 定位回 codes
                break
            # 更稳的写法：逐条查
        except Exception as e:
            self.write_log(f"[Base] get_option_quote 异常: {e}")

        # 逐条查询（最稳，避免 legs 顺序错位）
        for c in codes:
            try:
                from futu import OptionStrategyLeg
                legs = [OptionStrategyLeg(code=c, action="BUY", quantity=1.0)]
                ret, data = gw.quote_ctx.get_option_quote(legs)
                if ret != 0 or data is None or data.empty:
                    continue
                r = data.iloc[0]
                code_val = str(r.get("code", c))
                result[code_val] = {
                    "code":               code_val,
                    "price":              self._f(r.get("price")),
                    "premium":            self._f(r.get("premium")),
                    "implied_volatility": self._f(r.get("implied_volatility")),
                    "delta":              self._f(r.get("delta")),
                    "gamma":              self._f(r.get("gamma")),
                    "vega":               self._f(r.get("vega")),
                    "theta":              self._f(r.get("theta")),
                    "rho":                self._f(r.get("rho")),
                    "option_type":        str(r.get("option_type", "")).upper(),
                    "expire_time":        str(r.get("expire_time", "")),
                    "strike_price":       self._f(r.get("strike_price")),
                    "contract_size":      self._f(r.get("contract_size", 100)),
                    "contract_multiplier":self._f(r.get("contract_multiplier", 100)),
                    "days_to_expiry":     int(r.get("days_to_expiry", 0) or 0),
                    "intrinsic_value":    self._f(r.get("intrinsic_value")),
                    "time_value":         self._f(r.get("time_value")),
                    "breakeven_point":    r.get("breakeven_point", None),
                    "dist_to_breakeven":  r.get("dist_to_breakeven", None),
                    "prob_of_profit":     self._f(r.get("prob_of_profit")),  # 百分比 0-100
                    "seller_roi":         r.get("seller_roi", None),
                    "mark_price":         self._f(r.get("mark_price")),
                    "open_interest":      r.get("open_interest", "N/A"),
                }
            except Exception as e:
                self.write_log(f"[Base] 报价单条异常 {c}: {e}")
        return result

    def _query_full_chain(self, code: str) -> List[dict]:
        """完整流程：到期日 → 合约列表 → 批量报价 → 合并为带希腊值的合约列表"""
        merged: List[dict] = []
        dates = self._query_expiry_dates(code)
        if not dates:
            return merged
        # 只取 [min_days, max_days] 区间内的到期日
        target_dates = [d["strike_time"] for d in dates
                        if self.min_days_to_expiry <= d["distance"] <= self.max_days_to_expiry]
        if not target_dates:
            # 兜底：至少取最近的
            target_dates = [dates[0]["strike_time"]]
        all_codes: List[str] = []
        code_to_meta: Dict[str, dict] = {}
        for dt in target_dates:
            chain = self._query_chain_by_date(code, dt)
            for c in chain:
                if c["suspension"]:
                    continue
                all_codes.append(c["code"])
                code_to_meta[c["code"]] = c
        if not all_codes:
            return merged
        quotes = self._batch_quote(all_codes)
        for c in all_codes:
            meta = code_to_meta.get(c, {})
            q = quotes.get(c, {})
            merged.append({
                **meta,
                **q,
                # 兼容字段（旧代码用的名字）
                "otm_prob":    q.get("prob_of_profit", 0) / 100.0,   # 转 0-1
                "annual_roi":  self._f(meta.get("premium")) / max(self._f(meta.get("strike_price")), 1)
                                * (365.0 / max(q.get("days_to_expiry", 30), 1)),
                "iv":          q.get("implied_volatility", 0),
                "mid_price":   q.get("mark_price", q.get("price", 0)),
            })
        self._quote_cache = {m["code"]: m for m in merged}
        self._quote_cache_ts = time.time()
        self.write_log(f"[Base] 期权链合并完成 {code}: {len(merged)} 条（含希腊值）")
        return merged

    # ── 筛选工具 ──────────────────────────────────────────
    def _select_contracts(self, chain: List[dict], leg_type: str) -> List[dict]:
        """按类型(call/put) + 到期天数 + otm_prob + 年化ROI 筛选"""
        out = []
        for item in chain:
            if leg_type == "call" and not item.get("is_call"): continue
            if leg_type == "put"  and not item.get("is_put"):  continue
            dte = item.get("days_to_expiry", 999)
            if not (self.min_days_to_expiry <= dte <= self.max_days_to_expiry):
                continue
            if item.get("otm_prob", 0) < self.min_otm_prob:
                continue
            if item.get("annual_roi", 0) < self.min_annual_roi:
                continue
            out.append(item)
        return out

    def _find_nearest_delta(self, chain: List[dict], target_delta: float,
                            leg_type: str) -> Optional[dict]:
        """在 chain 中找 |delta - target| 最小的合约"""
        best, best_diff = None, 999
        for c in chain:
            if leg_type == "call" and not c.get("is_call"): continue
            if leg_type == "put"  and not c.get("is_put"):  continue
            d = abs(abs(c.get("delta", 0)) - abs(target_delta))
            if d < best_diff:
                best_diff, best = d, c
        return best

    # ── 下单 / 平仓 / 展期 ────────────────────────────────
    def _send_option_order(self, leg: dict, direction: Direction,
                           offset: Offset) -> bool:
        name = leg.get("name", "leg")
        if name in self.legs and not offset == Offset.CLOSE:
            # 同名腿已存在（未平仓），不重复开
            return False
        try:
            from vnpy.trader.object import OrderRequest
            limit_price = leg.get("limit_price",
                          leg.get("mid_price",
                          leg.get("mark_price",
                          leg.get("price", 0))))
            vol = int(leg.get("size", self.position_size))
            req = OrderRequest(
                symbol=leg["code"],
                exchange=leg.get("exchange", Exchange.SMART),
                direction=direction,
                type=OrderType.LIMIT,
                volume=vol,
                price=limit_price,
                offset=offset,
                reference=f"option_{self.strategy_name}",
            )
            gw_name = "FUTU_US" if ".US." in self.vt_symbol or \
                       self.vt_symbol.endswith(".SMART") else "FUTU_HK"
            vt_oid = self.cta_engine.main_engine.send_order(req, gw_name)
            if vt_oid:
                leg["vt_orderid"] = vt_oid
                leg["direction"]  = direction
                leg["offset"]     = offset
                self.legs[name] = leg
                self._retry_count.pop(name, None)
                self.write_log(f"[Base] 下单 {direction.value} {leg['code']} "
                               f"x{vol} @{limit_price:.2f} oid={vt_oid}")
                return True
            else:
                self.write_log(f"[Base] 下单返回空 oid: {leg['code']}")
                return False
        except Exception as e:
            self.write_log(f"[Base] 下单异常 {leg.get('code')}: {e}")
            return False

    def _open_spread(self, long_leg: dict, short_leg: dict) -> bool:
        """先开 long，失败则整体失败；short 失败则回滚 long"""
        long_ok = self._send_option_order(long_leg, Direction.LONG, Offset.OPEN)
        if not long_ok:
            return False
        short_ok = self._send_option_order(short_leg, Direction.SHORT, Offset.OPEN)
        if not short_ok:
            self.write_log("[Base] spread short 腿失败，回滚 long")
            self._send_option_order(long_leg, Direction.SHORT, Offset.CLOSE)
            return False
        self.net_premium = (long_leg.get("premium", 0)
                          - short_leg.get("premium", 0))
        return True

    def _close_all_legs(self):
        """平仓所有腿（方向取反）"""
        for name, leg in list(self.legs.items()):
            is_long = leg.get("is_long", leg.get("direction") == Direction.LONG)
            close_dir = Direction.SHORT if is_long else Direction.LONG
            self._send_option_order(leg, close_dir, Offset.CLOSE)
        self.legs.clear()

    def _roll_positions(self):
        """展期：先平旧腿，再等 on_bar 重新开仓"""
        self.write_log("[Base] 展期：平仓近月")
        self._close_all_legs()

    def _manage_expiry(self, bar: BarData) -> bool:
        """距到期<=3天强制平仓"""
        for name, leg in list(self.legs.items()):
            dte = leg.get("days_to_expiry", 999)
            if dte <= 3:
                self.write_log(f"[Base] {name} 临近到期({dte}d) 强平")
                self._close_all_legs()
                return True
        return False

    # ── 现金 / Regime ────────────────────────────────────
    def _get_available_cash(self) -> float:
        for gw_name in ("FUTU_US", "FUTU_HK", "FUTU"):
            gw = self.cta_engine.main_engine.get_gateway(gw_name)
            if gw and hasattr(gw, "acc_info") and gw.acc_info.get("cash"):
                return float(gw.acc_info["cash"])
        return 0.0

    def _scaled_size(self, base_size: int = None) -> int:
        """按 Regime 缩放仓位"""
        base = base_size or self.position_size
        if self.regime_label == "bull_trend":    return int(base * 1.2)
        if self.regime_label == "bear_trend":    return int(base * 0.8)
        if self.regime_label == "high_volatility":return int(base * 0.6)
        return base

    def _check_cash(self, required_per_contract: float) -> bool:
        need = required_per_contract * self._scaled_size() * (1 + self.cash_buffer_ratio)
        avail = self._get_available_cash()
        if avail <= 0:
            return True  # 无法判断时放行
        return avail >= need

    # ── 工具 ──────────────────────────────────────────────
    @staticmethod
    def _f(val):
        try:
            s = str(val).strip()
            if s == "" or s.upper() == "N/A":
                return 0.0
            return float(s)
        except:
            return 0.0

    def _estimate_pnl(self) -> float:
        """用最新报价估浮动盈亏"""
        total = 0.0
        quotes = self._batch_quote(list(self.legs.keys()))
        for name, leg in self.legs.items():
            q = quotes.get(leg["code"], {})
            cur = q.get("price", leg.get("premium", 0))
            entry = leg.get("premium", cur)
            is_long = leg.get("is_long",
                       leg.get("direction") == Direction.LONG)
            total += (cur - entry) if is_long else (entry - cur)
        self.pnl = total
        return total
