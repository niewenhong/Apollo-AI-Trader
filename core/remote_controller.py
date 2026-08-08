# -*- coding: utf-8 -*-
"""
core/remote_controller.py - 远程控制处理器 v3.8.4-fix5
修复：
  - /account 直接通过富途网关直查真实账户数据
  - 港股综合账户购买力 = (现金 + 证券市值) × 杠杆倍数（默认2倍）
  - 美股购买力直接用富途返回值
  - 所有其他命令保持不变，零回退
"""
import logging
import time
import traceback

logger = logging.getLogger("RemoteController")

# 港股综合账户杠杆倍数（融资账户购买力 = 现金 × 倍数）
HK_MARGIN_LEVERAGE = 2.0


class RemoteController:
    """Telegram 远程控制器（双引擎版 v3.8.4-fix5）"""

    def __init__(self, db=None, notifier=None, config: dict = None,
                 account_manager=None, order_manager=None):
        self.db = db
        self.notifier = notifier
        self.config = config or {}
        self.account_manager = account_manager
        self.order_manager = order_manager
        self.main_engine_us = None
        self.main_engine_hk = None
        self.main_engine = None
        self.strategy_engine = None
        self.registry = None
        self._authorized = True
        self._gateways = {}

    # ===== 注入接口 =====

    def set_main_engines(self, me_us, me_hk):
        self.main_engine_us = me_us
        self.main_engine_hk = me_hk
        self.main_engine = me_us
        self._gateways = {}
        for me in [me_us, me_hk]:
            if me is None:
                continue
            gw_dict = getattr(me, 'gateways', {})
            if gw_dict:
                self._gateways.update(gw_dict)
        logger.info(f"[Remote] 网关注入完成: {list(self._gateways.keys())}")

    def set_main_engine(self, me):
        self.set_main_engines(me, me)

    def set_strategy_engine(self, engine):
        self.strategy_engine = engine

    def set_registry(self, registry):
        self.registry = registry

    # ===== 命令分发 =====

    def handle_command(self, command: str, args: list) -> str:
        cmd = (command or "").strip().lower().lstrip("/")
        normalized = "/" + cmd
        handlers = {
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/boot": self._cmd_boot,
            "/add": self._cmd_add,
            "/remove": self._cmd_remove,
            "/rollback": self._cmd_rollback,
            "/reload": self._cmd_reload,
            "/cluster": self._cmd_cluster,
            "/shutdown": self._cmd_shutdown,
            "/stop_all": self._cmd_stop_all,
            "/positions": self._cmd_positions,
            "/account": self._cmd_account,
        }
        handler = handlers.get(normalized)
        if handler:
            try:
                return handler(args)
            except Exception as e:
                logger.exception("命令执行异常")
                return f"❌ 命令执行异常: {str(e)[:200]}"
        return f"❓ 未知命令: {command}\n输入 /help 查看可用命令"

    # ===== 命令实现 =====

    def _cmd_help(self, args) -> str:
        return (
            "📋 <b>Apollo 命令列表</b> (v3.8.4)\n"
            "/help - 显示帮助\n"
            "/status - 策略状态\n"
            "/boot &lt;pwd&gt; - 全量重启策略\n"
            "/add &lt;name&gt; &lt;class&gt; &lt;symbol&gt; &lt;market&gt; &lt;params_json&gt; &lt;pwd&gt;\n"
            "/remove &lt;name&gt; &lt;pwd&gt; - 停止并移除策略\n"
            "/rollback &lt;name&gt; &lt;version&gt; &lt;pwd&gt; - 回滚参数\n"
            "/reload &lt;pwd&gt; - 手动热加载\n"
            "/cluster - 集群状态\n"
            "/stop_all &lt;pwd&gt; - 停止所有策略\n"
            "/positions - 持仓查询（双市场）\n"
            "/account - 账户资金（双市场）\n"
            "/shutdown &lt;pwd&gt; - 安全关闭系统"
        )

    def _cmd_status(self, args) -> str:
        if self.strategy_engine:
            if hasattr(self.strategy_engine, 'format_status'):
                try:
                    return self.strategy_engine.format_status()
                except Exception as e:
                    logger.warning(f"format_status 调用失败: {e}")
            lines = ["📊 <b>策略状态</b>"]
            strategies = getattr(self.strategy_engine, 'strategies', {})
            if not strategies:
                lines.append("  无运行中策略")
            else:
                for name, s in strategies.items():
                    vt = getattr(s, 'vt_symbol', '?')
                    tr = getattr(s, 'trading', False)
                    p = getattr(s, 'pos', 0)
                    icon = "🟢" if tr else "🔴"
                    lines.append(f"  {icon} {name} ({vt}) pos={p}")
            return "\n".join(lines)
        return "❌ 策略引擎未注入"

    def _cmd_boot(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /boot &lt;password&gt;"
        if args[0] != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if not self.strategy_engine:
            return "❌ 策略引擎未初始化"
        result = self.strategy_engine.boot(operator="telegram:boot")
        deployed = result.get('deployed', [])
        failed = result.get('failed', [])
        return f"🔄 启动完成: 成功 {len(deployed)}，失败 {len(failed)}"

    def _cmd_add(self, args) -> str:
        if len(args) < 6:
            return "❌ 用法: /add &lt;name&gt; &lt;class&gt; &lt;symbol&gt; &lt;market&gt; &lt;params_json&gt; &lt;pwd&gt;"
        pwd = args[-1]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        name, cls, symbol, market = args[0], args[1], args[2], args[3]
        try:
            import json
            params = json.loads(args[4])
        except Exception:
            return "❌ params_json 解析失败"
        if self.strategy_engine:
            ok = self.strategy_engine.add_strategy(
                name, cls, symbol, market, params,
                source="telegram", modifier="telegram:add"
            )
            return f"{'✅' if ok else '❌'} 策略 {name} {'添加成功' if ok else '添加失败'}"
        return "❌ 策略引擎未初始化"

    def _cmd_remove(self, args) -> str:
        if len(args) < 2:
            return "❌ 用法: /remove &lt;name&gt; &lt;pwd&gt;"
        pwd = args[-1]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        name = args[0]
        if self.strategy_engine:
            ok = self.strategy_engine.remove_strategy(name, operator="telegram:remove")
            return f"{'✅' if ok else '❌'} 策略 {name} {'已移除' if ok else '移除失败'}"
        return "❌ 策略引擎未初始化"

    def _cmd_rollback(self, args) -> str:
        if len(args) < 3:
            return "❌ 用法: /rollback &lt;name&gt; &lt;version&gt; &lt;pwd&gt;"
        pwd = args[-1]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        name = args[0]
        try:
            version = int(args[1])
        except Exception:
            return "❌ version 必须是数字"
        if self.strategy_engine:
            ok = self.strategy_engine.rollback(name, version, operator="telegram")
            return f"{'✅' if ok else '❌'} 回滚 {name} → v{version} {'成功' if ok else '失败'}"
        return "❌ 策略引擎未初始化"

    def _cmd_reload(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /reload &lt;pwd&gt;"
        if args[0] != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.strategy_engine:
            changed = self.strategy_engine.check_and_reload_changed(operator="telegram:reload")
            return f"🔄 热加载完成，处理: {changed if changed else '无变化'}"
        return "❌ 策略引擎未初始化"

    def _cmd_cluster(self, args) -> str:
        lines = ["🖥️ <b>集群状态</b>"]
        tag = "LOCAL"
        if self.registry:
            tag = getattr(self.registry, 'tag', 'LOCAL')
            if callable(tag):
                tag = tag()
        lines.append(f"  🏷️ 本机: {tag}")
        mode = "N/A"
        if self.registry:
            mode = getattr(self.registry, 'heartbeat_mode',
                          getattr(self.registry, 'mode', 'N/A'))
        lines.append(f"  🔒 模式: {mode}")
        inst = "N/A"
        if self.registry:
            inst = getattr(self.registry, 'instance_type', 'STANDALONE')
        lines.append(f"  🖥️ 类型: {inst}")
        members = []
        if self.registry and hasattr(self.registry, 'discover'):
            try:
                members = self.registry.discover()
            except Exception:
                members = []
        if members:
            lines.append("  ── 其他节点 ──")
            for m in members:
                lines.append(f"  🟢 {m.get('machine_id','?')} [{m.get('instance_type','?')}] up={m.get('uptime','?')}")
        else:
            lines.append("  (单机模式，无其他节点)")
        return "\n".join(lines)

    def _cmd_shutdown(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /shutdown &lt;pwd&gt;"
        if args[0] != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.notifier:
            try:
                self.notifier.send_shutdown_notice(reason="Telegram 远程指令")
            except Exception:
                pass
        if self.strategy_engine:
            try:
                self.strategy_engine.stop_all()
            except Exception:
                pass
        import os, threading
        def _delayed_exit():
            time.sleep(2)
            os._exit(0)
        threading.Thread(target=_delayed_exit, daemon=True).start()
        return "🛑 系统正在关闭..."

    def _cmd_stop_all(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /stop_all &lt;pwd&gt;"
        if args[0] != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.strategy_engine:
            self.strategy_engine.stop_all()
            return "⏹️ 所有策略已停止"
        return "❌ 策略引擎未初始化"

    # ===== 持仓查询（直查富途网关） =====

    def _cmd_positions(self, args) -> str:
        if not self._gateways:
            return "❌ 网关注入失败，无法查询持仓"

        from futu import RET_OK
        all_positions = []
        for gw_name, gw in self._gateways.items():
            trd_ctx = getattr(gw, 'trd_ctx', None) or getattr(gw, 'trade_ctx', None)
            if trd_ctx is None:
                continue
            acc_id = getattr(gw, 'acc_id', 0)
            env = getattr(gw, 'env', None)
            try:
                ret, data = trd_ctx.position_list_query(trd_env=env, acc_id=acc_id)
                if ret != RET_OK or data is None or data.empty:
                    continue
                for _, row in data.iterrows():
                    qty = int(row.get("qty", 0))
                    if qty == 0:
                        continue
                    code = row.get("code", "?")
                    all_positions.append({
                        "symbol": code,
                        "qty": qty,
                        "cost": float(row.get("cost_price", 0)),
                        "pnl": float(row.get("pl_val", 0)),
                        "gateway": gw_name,
                    })
            except Exception as e:
                logger.warning(f"网关 {gw_name} 持仓查询失败: {e}")

        if not all_positions:
            return "⚠️ 当前没有任何持仓"
        lines = ["📦 <b>持仓查询</b> (双市场)"]
        lines.append("─" * 30)
        total_pnl = 0.0
        for pos in all_positions:
            total_pnl += pos["pnl"]
            lines.append(f"· {pos['symbol']} [{pos['gateway']}]")
            lines.append(f"  量:{pos['qty']} 成本:{pos['cost']:.2f} 盈亏:{pos['pnl']:+.2f}")
        lines.append("─" * 30)
        lines.append(f"💰 持仓总盈亏: {total_pnl:+.2f}")
        return "\n".join(lines)

    # ===== ★ 资金查询（直查富途网关 + 港股杠杆修正） =====

    def _cmd_account(self, args) -> str:
        if not self._gateways:
            return "❌ 网关注入失败，无法查询资金"

        from futu import RET_OK
        accounts = []  # 每个元素: dict with gateway/currency/total/cash/market/frozen/power

        for gw_name, gw in self._gateways.items():
            trd_ctx = getattr(gw, 'trd_ctx', None) or getattr(gw, 'trade_ctx', None)
            if trd_ctx is None:
                logger.warning(f"网关 {gw_name} 无交易上下文，跳过")
                continue
            acc_id = getattr(gw, 'acc_id', 0)
            env = getattr(gw, 'env', None)
            if acc_id == 0:
                logger.warning(f"网关 {gw_name} acc_id=0，跳过")
                continue
            try:
                ret, data = trd_ctx.accinfo_query(trd_env=env, acc_id=acc_id)
                if ret != RET_OK or data is None or data.empty:
                    logger.warning(f"网关 {gw_name} 资金查询返回空")
                    continue
                for _, row in data.iterrows():
                    currency = str(row.get("currency", "?"))
                    cash = float(row.get("cash", 0))
                    market_val = float(row.get("market_val", 0))
                    frozens = float(row.get("frozen_cash", 0))
                    power_raw = float(row.get("power", 0))
                    total = float(row.get("total_assets", cash + market_val))

                    # ★ 港股综合账户：购买力 = 现金 × 杠杆倍数
                    if currency == "HKD":
                        power = round(cash * HK_MARGIN_LEVERAGE, 2)
                        power_note = f" (现金×{HK_MARGIN_LEVERAGE:.0f}倍杠杆)"
                    else:
                        power = power_raw
                        power_note = ""

                    accounts.append({
                        "gateway": gw_name,
                        "currency": currency,
                        "total_assets": total,
                        "cash": cash,
                        "market_val": market_val,
                        "frozen_cash": frozens,
                        "power": power,
                        "power_note": power_note,
                    })
                    logger.info(
                        f"[Remote] {gw_name} 资金: 总资产={total} 现金={cash} "
                        f"证券={market_val} 购买力={power}{power_note} 货币={currency}"
                    )
            except Exception as e:
                logger.warning(f"网关 {gw_name} 资金查询失败: {e}")

        if not accounts:
            return "⚠️ 未获取到账户信息"

        # 组装输出
        lines = ["💰 <b>账户资金</b> (双市场)"]
        lines.append("─" * 30)
        for acc in accounts:
            gw = acc['gateway']
            cur = acc['currency']
            if cur == "USD":
                flag = "🇺🇸 美股"
            elif cur == "HKD":
                flag = "🇭🇰 港股"
            else:
                flag = f"({cur})"
            lines.append(f"{flag} {gw} ({cur})")
            lines.append(f"  总资产: {acc['total_assets']:>14,.2f} {cur}")
            lines.append(f"  现金:   {acc['cash']:>14,.2f} {cur}")
            lines.append(f"  证券:   {acc['market_val']:>14,.2f} {cur}")
            lines.append(f"  冻结:   {acc['frozen_cash']:>14,.2f} {cur}")
            lines.append(f"  购买力: {acc['power']:>14,.2f} {cur}{acc['power_note']}")
            lines.append("")
        return "\n".join(lines).strip()

    # ===== 工具函数 =====

    def _format_money(self, value: float) -> str:
        """统一金额格式化"""
        try:
            return f"{value:,.2f}"
        except Exception:
            return str(value)
