"""
Apollo-AI-Trader v2.0 - 牛熊证策略
功能：
  1. MA200 判断市场 regime
  2. Bear regime 时选杠杆牛证买入
  3. 回收价触发自动平仓
  4. 15:55 后不再开新仓（收市竞价）
  5. 参数全部热更新
"""

import time
from datetime import datetime
from pathlib import Path
import pytz
from futu import OpenQuoteContext, ReferenceType
from strategies.base_cta_strategy import BaseCtaStrategy
from core.utils import vt_to_futu
from core.market_router import detect_market


class CbbcStrategy(BaseCtaStrategy):
    """港股牛熊证策略"""

    bull_entry_pct = -2.0
    bull_exit_pct = 1.0
    leverage_min = 8.0
    max_bulls = 2
    fees = 0.0013
    issuer_top3 = None
    recovery_buffer_pct = 1.5

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.futu_code = vt_to_futu(vt_symbol)
        self.ma200 = None
        self.regime = "neutral"
        self.bulls_held = []
        self._cbbc_cache = []
        self._cbbc_cache_date = ""
        self.log_path = Path("logs") / f"cbbc_{vt_symbol.replace('.','_')}.csv"
        self._ensure_log()
        self._q = None

    def _ensure_log(self):
        if not self.log_path.exists():
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "w") as f:
                f.write("time,symbol,action,cbbc_code,price,reason\n")

    def _log(self, action, cbbc_code, price, reason=""):
        line = (f"{datetime.now().isoformat()},{self.vt_symbol},"
                f"{action},{cbbc_code},{price},{reason}\n")
        with open(self.log_path, "a") as f:
            f.write(line)

    def on_init(self):
        self._q = OpenQuoteContext("127.0.0.1", 11111)
        self._calc_ma200()
        self.write_log(f"🟢 CBBC 初始化: ma200={self.ma200}")
        super().on_init()

    def on_stop(self):
        if self._q:
            self._q.close()
        self._log("shutdown", "", 0, "")
        self.write_log("⏹️ CBBC 已停止")
        super().on_stop()

    def _calc_ma200(self):
        try:
            ret, snap, _ = self._q.get_market_snapshot(self.futu_code)
            if ret == 0 and snap is not None and not snap.empty:
                self.ma200 = float(snap["ma_200"].iloc[0])
            else:
                self.ma200 = None
        except Exception as e:
            self.write_log(f"⚠️ MA200 获取失败: {e}")
            self.ma200 = None

    def _refresh_cbbc_chain(self):
        try:
            ret, df, _ = self._q.get_referencest_data(
                self.futu_code, "CBBC")
            if ret != 0 or df is None or df.empty:
                self._cbbc_cache = []
                return
            bulls = df[df["type"] == "Bull"].copy()
            bulls = bulls[bulls["leverage"] >= self.leverage_min]
            if self.issuer_top3:
                bulls = bulls[bulls["issuer"].isin(self.issuer_top3)]
            self._cbbc_cache = bulls.to_dict("records")
        except Exception as e:
            self.write_log(f"⚠️ CBBC 链刷新失败: {e}")
            self._cbbc_cache = []

    def _select_bull(self, price):
        if not self._cbbc_cache:
            return None
        candidates = [
            c for c in self._cbbc_cache
            if c["recovery_price"] < price * (1 - self.recovery_buffer_pct / 100)
            and c["strike_price"] < price
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda c: abs(
            c["strike_price"] - (self.ma200 or price)))
        return candidates[0]

    def _on_tick_impl(self, tick):
        price = tick.last_price
        if price is None or price <= 0:
            return

        # 15:55 后不再开新仓
        if detect_market(self.vt_symbol) == "hk":
            hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong")).time()
            if hk_now.hour == 15 and hk_now.minute >= 55:
                # 只处理回收
                for b in self.bulls_held[:]:
                    if price <= b["recovery"]:
                        self._log("recovery", b["cbbc_code"], price,
                                  f"recovery={b['recovery']}")
                        self.bulls_held.remove(b)
                return

        today = datetime.now().strftime("%Y-%m-%d")
        if self._cbbc_cache_date != today:
            self._calc_ma200()
            self._refresh_cbbc_chain()
            self._cbbc_cache_date = today

        if self.ma200 is None:
            return

        dist = (price - self.ma200) / self.ma200 * 100
        if dist > 2.0:
            self.regime = "bull"
        elif dist < -2.0:
            self.regime = "bear"
        else:
            self.regime = "neutral"

        self.write_log(f"📊 regime={self.regime} dist={dist:+.2f}% price={price:.2f}")

        # 回收检查
        for b in self.bulls_held[:]:
            if price <= b["recovery"]:
                self._log("recovery", b["cbbc_code"], price,
                          f"recovery={b['recovery']}")
                self.bulls_held.remove(b)

        if self.regime == "bear" and len(self.bulls_held) < self.max_bulls:
            best = self._select_bull(price)
            if best:
                self.safe_buy(best["strike_price"] * 0.98, 10000)
                self.bulls_held.append({
                    "cbbc_code": best["code"],
                    "strike": best["strike_price"],
                    "recovery": best["recovery_price"],
                    "entry_price": best["strike_price"] * 0.98,
                    "qty": 10000,
                })
                self._log("buy", best["code"], best["strike_price"],
                          f"lev={best['leverage']} recov={best['recovery_price']}")
        elif self.regime == "bull" and self.bulls_held:
            for b in self.bulls_held[:]:
                profit_pct = ((price - b["entry_price"]) / b["entry_price"]
                               * 100 - self.fees * 200)
                if dist >= self.bull_exit_pct or profit_pct >= 8.0:
                    self.safe_sell(price, b["qty"])
                    self._log("sell", b["cbbc_code"], price,
                              f"profit={profit_pct:.2f}%")
                    self.bulls_held.remove(b)

    def update_config(self, new_params):
        changed = []
        for key in ["bull_entry_pct", "bull_exit_pct", "leverage_min",
                     "max_bulls", "fees", "recovery_buffer_pct",
                     "issuer_top3"]:
            if key in new_params:
                old = getattr(self, key, None)
                new = new_params[key]
                if old != new:
                    setattr(self, key, new)
                    changed.append(f"{key}: {old}→{new}")
        if changed:
            msg = "; ".join(changed)
            self.write_log(f"🔥 CBBC 热更新: {msg}")
            if self.notifier:
                self.notifier.send_notification(
                    self.vt_symbol, 0.0, "🔥 CBBC 热更新", msg)
            self._cbbc_cache_date = ""  # 强制刷新

    def get_snapshot(self):
        snap = super().get_snapshot()
        snap.update({
            "ma200": self.ma200,
            "regime": self.regime,
            "bulls_held": len(self.bulls_held),
            "cbbc_cache_size": len(self._cbbc_cache),
        })
        return snap
