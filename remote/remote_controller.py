"""
remote/remote_controller.py - Apollo-AI-Trader v2.6.0
远程控制器：Telegram命令处理 + AI审核确认 + 参数优化触发
新增命令：
  /ai_select    - 立即执行AI选股
  /diagnose SYM - 诊股
  /optimize SYM - 优化参数
  /review SYM   - 查看审核历史
  /pool         - 查看执行池
  /params SYM   - 查看AI建议参数
"""
import logging
import time
import traceback
from datetime import datetime

logger = logging.getLogger("RemoteController")


class RemoteController:
    def __init__(self, notifier, config: dict):
        self.notifier = notifier
        self.config = config
        self.cta_engine = None
        self.cta_engines = {}
        self.db = None
        self.decision_engine = None
        self.optimizer = None
        self.strategy_classes = {}
        self.strategy_config = None
        self._shutdown_cb = None
        self._enable_extended_cb = None
        self._pending_orders = {}
        logger.info("[RemoteController] v2.6.0 initialized")

    def set_shutdown_callback(self, cb): self._shutdown_cb = cb
    def set_enable_extended_callback(self, cb): self._enable_extended_cb = cb
    @property
    def on_enable_extended(self): return self._enable_extended_cb

    # ── 状态查询 ──────────────────────────────────
    def get_status_text(self) -> str:
        lines = [f"📊 Apollo v2.6.0 [{datetime.now().strftime('%H:%M:%S')}]"]
        for tag, eng in self.cta_engines.items():
            try:
                total = len(eng.strategies)
                active = sum(1 for s in eng.strategies.values() if getattr(s,'active',False))
                lines.append(f"  {tag}: {active}/{total} 运行中")
            except Exception as e:
                lines.append(f"  {tag}: 查询失败 ({e})")
        if self.db:
            pool = self.db.get_active_pool()
            lines.append(f"  🤖 AI选股池: {len(pool)} 只")
        lines.append(f"  延伸时段: {'✅开' if self.config.get('allow_extended_hours') else '⏸关'}")
        return "\n".join(lines)

    def get_positions_text(self) -> str:
        lines = ["📦 持仓"]
        total = 0
        for tag, eng in self.cta_engines.items():
            for name, s in eng.strategies.items():
                pos = getattr(s,'pos',0)
                if pos != 0:
                    lines.append(f"  {tag}/{name}: {pos}")
                    total += abs(pos)
        if total == 0: lines.append("  空仓")
        lines.append(f"合计: {total} 股")
        return "\n".join(lines)

    def get_pnl_text(self) -> str:
        lines = ["💰 盈亏"]
        total = 0.0
        for tag, eng in self.cta_engines.items():
            for name, s in eng.strategies.items():
                pnl = getattr(s,'today_pnl',0.0)
                if pnl != 0:
                    lines.append(f"  {tag}/{name}: {pnl:+.2f}")
                    total += pnl
        lines.append(f"合计: {total:+.2f}")
        return "\n".join(lines)

    # ── 策略管理 ──────────────────────────────────
    def list_strategies(self, market="") -> str:
        lines = []
        engines = {market:self.cta_engines[market]} if market else self.cta_engines
        for tag, eng in engines.items():
            lines.append(f"\n📍 {tag}:")
            for name, s in eng.strategies.items():
                active = "🟢" if getattr(s,'active',False) else "⚪"
                pos = getattr(s,'pos',0)
                lines.append(f"  {active} {name} pos={pos}")
        return "\n".join(lines) if lines else "无策略"

    def pause_strategy(self, market, name) -> str:
        eng = self.cta_engines.get(market.upper())
        if not eng: return f"❌ 未知市场: {market}"
        if name not in eng.strategies: return f"❌ 未找到: {name}"
        try:
            eng.stop_strategy(name)
            return f"⏸ {market}/{name}"
        except Exception as e:
            return f"❌ {e}"

    def resume_strategy(self, market, name) -> str:
        eng = self.cta_engines.get(market.upper())
        if not eng: return f"❌ 未知市场: {market}"
        if name not in eng.strategies: return f"❌ 未找到: {name}"
        try:
            eng.start_strategy(name)
            return f"▶️ {market}/{name}"
        except Exception as e:
            return f"❌ {e}"

    def add_strategy(self, market, symbol, strategy_class_name="MultiIndicatorStrategy") -> str:
        eng = self.cta_engines.get(market.upper())
        if not eng: return f"❌ 未知市场: {market}"
        cls = self.strategy_classes.get(strategy_class_name)
        if not cls: return f"❌ 策略类未注册: {strategy_class_name}"
        name = f"{strategy_class_name}_{market.upper()}_{symbol}"
        if name in eng.strategies: return f"⚠️ 已存在: {name}"
        sc = self.strategy_config or {}
        params = (sc.get("default_setting") or {}).copy()
        params.update((sc.get("per_market_overrides") or {}).get(market.upper(),{}))
        params.update((sc.get("per_symbol_overrides") or {}).get(symbol,{}))
        params["market"] = market.upper()
        vt = f"{symbol}.SMART" if market.upper()=="US" else f"{symbol}.SEHK"
        try:
            eng.classes[cls.__name__] = cls
            eng.add_strategy(cls.__name__, name, vt, params)
            eng.init_strategy(name); eng.start_strategy(name)
            # 加入执行池
            if self.db: self.db.add_to_pool(vt, market.upper())
            return f"✅ {name} ({vt})"
        except Exception as e:
            return f"❌ {e}\n{traceback.format_exc()}"

    def remove_strategy(self, market, name) -> str:
        eng = self.cta_engines.get(market.upper())
        if not eng: return f"❌ 未知市场: {market}"
        if name not in eng.strategies: return f"❌ 未找到: {name}"
        try:
            eng.remove_strategy(name)
            return f"🗑 {market}/{name}"
        except Exception as e:
            return f"❌ {e}"

    # ── 🆕 AI命令 ──────────────────────────────────
    def ai_select_now(self) -> str:
        """立即执行AI选股"""
        if not self.db: return "❌ 数据库未初始化"
        try:
            from ai.stock_selector import AIStockSelector
            # 获取quote_ctx
            gw = None
            for eng in self.cta_engines.values():
                for g in eng.main_engine.gateways.values():
                    if hasattr(g, "quote_ctx"): gw = g; break
            if not gw: return "❌ 无行情连接"
            selector = AIStockSelector(gw.quote_ctx, self.db, top_n=20)
            results = selector.select()
            lines = [f"🤖 AI选股完成: {len(results)} 只"]
            for r in results[:15]:
                lines.append(f"  {r['vt_symbol']}: {r['score']:.0f}分 {r.get('reason','')[:30]}")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 选股失败: {e}"

    def diagnose_symbol(self, symbol: str) -> str:
        """诊股"""
        if not self.db: return "❌ DB未初始化"
        try:
            gw = None
            for eng in self.cta_engines.values():
                for g in eng.main_engine.gateways.values():
                    if hasattr(g, "quote_ctx"): gw = g; break
            if not gw: return "❌ 无行情连接"
            from ai.stock_diagnosis import StockDiagnosis
            diag = StockDiagnosis(gw.quote_ctx, self.db)
            code = f"US.{symbol}" if not "." in symbol else symbol
            result = diag.diagnose(code)
            return f"🩺 {code}: {result.get('summary','')}"
        except Exception as e:
            return f"❌ 诊股失败: {e}"

    def optimize_symbol(self, symbol: str) -> str:
        """优化参数"""
        if not self.optimizer: return "❌ 优化器未初始化"
        try:
            vt = f"{symbol}.SMART" if not "." in symbol else symbol
            current = {}
            grid = {"fast_ma":[5,10,15],"slow_ma":[20,30,40],
                    "rsi_period":[14,20],"stop_loss_pct":[0.015,0.02,0.03],
                    "take_profit_pct":[0.03,0.05,0.08]}
            result = self.optimizer.optimize(vt, "MultiIndicatorStrategy", grid, current)
            if result:
                return f"✅ {vt} 参数已优化: {result}"
            else:
                return f"⏸ {vt} 参数未通过审核"
        except Exception as e:
            return f"❌ 优化失败: {e}"

    def show_pool(self) -> str:
        """查看执行池"""
        if not self.db: return "❌ DB未初始化"
        pool = self.db.get_execution_pool()
        lines = [f"📋 执行池 ({len(pool)}只):"]
        for p in pool:
            lines.append(f"  {p['vt_symbol']} [{p['market']}] {p.get('status','')}")
        return "\n".join(lines) if pool else "执行池为空"

    def show_params(self, symbol: str) -> str:
        """查看AI建议参数"""
        if not self.db: return "❌ DB未初始化"
        vt = f"{symbol}.SMART" if not "." in symbol else symbol
        from ai.param_advisor import ParamAdvisor
        advisor = ParamAdvisor(self.db)
        params = advisor.get_applied_params(vt, "MultiIndicatorStrategy")
        if params:
            lines = [f"📝 {vt} AI参数:"]
            for k, v in params.items():
                if not k.startswith("_"): lines.append(f"  {k}={v}")
            return "\n".join(lines)
        return f"⚪ {vt} 无AI建议参数"

    # ── 系统控制 ──────────────────────────────────
    def shutdown(self):
        logger.info("[RC] 关机指令")
        if self._shutdown_cb: self._shutdown_cb()

    def health_check(self) -> str:
        lines = ["🏥 健康检查"]
        for tag, eng in self.cta_engines.items():
            lines.append(f"  {tag}: {'✅' if eng.strategies else '❌'}")
        if self.db:
            try:
                self.db.conn.execute("SELECT 1").fetchone()
                lines.append("  DB: ✅")
            except: lines.append("  DB: ❌")
        return "\n".join(lines)

    def toggle_ai(self, state: bool) -> str:
        self.config["llm_enabled"] = state
        return f"🧠 AI {'✅开' if state else '⏸关'}"

    # ── Debug交易 ──────────────────────────────────
    def debug_buy(self, market, symbol) -> str:
        from vnpy.trader.constant import Direction, OrderType, Offset, Exchange
        from vnpy.trader.object import OrderRequest
        tag = market.upper()
        if tag=="US" and not symbol.startswith("US."): symbol=f"US.{symbol}"
        elif tag=="HK" and not symbol.startswith("HK."): symbol=f"HK.{symbol}"
        exch = Exchange.SMART if tag=="US" else Exchange.SEHK
        req = OrderRequest(symbol=symbol, exchange=exch,
            direction=Direction.LONG, type=OrderType.LIMIT,
            volume=100, price=1.0, offset=Offset.NONE, reference="debug_buy")
        me = self._find_main(tag)
        if not me: return f"❌ 无MainEngine({tag})"
        gw_name = f"FUTU_{tag}"
        vt_oid = me.send_order(req, gw_name)
        if vt_oid:
            self._pending_orders[(tag,symbol)] = vt_oid
            return f"✅ debug_buy {symbol} oid={vt_oid}"
        return f"⚠️ 下单返回空"

    def debug_sell(self, market, symbol) -> str:
        from vnpy.trader.constant import Direction, OrderType, Offset, Exchange
        from vnpy.trader.object import OrderRequest
        tag = market.upper()
        if tag=="US" and not symbol.startswith("US."): symbol=f"US.{symbol}"
        elif tag=="HK" and not symbol.startswith("HK."): symbol=f"HK.{symbol}"
        exch = Exchange.SMART if tag=="US" else Exchange.SEHK
        req = OrderRequest(symbol=symbol, exchange=exch,
            direction=Direction.SHORT, type=OrderType.LIMIT,
            volume=100, price=1.0, offset=Offset.NONE, reference="debug_sell")
        me = self._find_main(tag)
        if not me: return f"❌ 无MainEngine({tag})"
        vt_oid = me.send_order(req, f"FUTU_{tag}")
        if vt_oid: return f"✅ debug_sell {symbol} oid={vt_oid}"
        return f"⚠️ 下单返回空"

    def debug_cancel(self, market, symbol) -> str:
        from vnpy.trader.object import CancelRequest
        from vnpy.trader.constant import Exchange, Status
        from futu import ModifyOrderOp
        tag = market.upper()
        if tag=="US" and not symbol.startswith("US."): full=f"US.{symbol}"
        elif tag=="HK" and not symbol.startswith("HK."): full=f"HK.{symbol}"
        else: full=symbol
        exch = Exchange.SMART if tag=="US" else Exchange.SEHK
        me = self._find_main(tag)
        if not me: return f"❌ 无MainEngine({tag})"
        gw_name = f"FUTU_{tag}"
        # 缓存
        cached = self._pending_orders.get((tag,full))
        if cached:
            bare = cached.split(".",1)[1] if "." in cached else cached
            me.cancel_order(CancelRequest(orderid=bare,symbol=full,exchange=exch), gw_name)
            del self._pending_orders[(tag,full)]
            return f"🗑 已撤 {full}"
        # orders
        canceled = 0
        for oid, o in list(getattr(me,'orders',{}).items()):
            if o.symbol not in {symbol,full}: continue
            if o.status in (Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED):
                bare = oid.split(".",1)[1] if "." in oid else oid
                me.cancel_order(CancelRequest(orderid=bare,symbol=o.symbol,exchange=o.exchange), gw_name)
                canceled += 1
        if canceled: return f"🗑 已撤 {full} ({canceled}笔)"
        return f"⚪ {full} 无活跃委托"

    def _find_main(self, tag):
        return self.cta_engines.get(tag,{}).main_engine if hasattr(self.cta_engines.get(tag,{}),"main_engine") else None
