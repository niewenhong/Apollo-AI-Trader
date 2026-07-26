"""
core/remote_controller.py — Telegram 远程控制 v2.8.2
修复：/positions 和 /account 直接通过 FutuGateway 内部的 trd_ctx 同步查询
（与 2.7.0 基线做法一致，绕过 vnpy 事件系统）
"""
import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger("RemoteController")


class RemoteController:
    """Telegram 远程控制器"""

    def __init__(self, db=None, notifier=None, config: dict = None):
        self.db = db
        self.notifier = notifier
        self.config = config or {}
        self.main_engine = None
        self.strategy_engine = None
        self.registry = None
        self._authorized = True
        # ★ 新增：直接持有网关引用，用于同步查询
        self._gateways = {}

    def set_main_engine(self, me):
        self.main_engine = me
        # 从 MainEngine 提取所有网关，缓存起来供查询用
        if me:
            self._gateways = me.gateways

    def set_strategy_engine(self, engine):
        self.strategy_engine = engine

    def set_registry(self, registry):
        self.registry = registry

    def handle_command(self, command: str, args: list) -> str:
        """命令分发 - 兼容带斜杠/不带斜杠"""
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
                return f"❌ 命令执行异常: {e}"
        return f"❓ 未知命令: {command}\n输入 /help 查看可用命令"

    # ═════════════════════════════════════════
    #  命令实现（2.8.0 原始逻辑，未改动）
    # ═════════════════════════════════════════
    def _cmd_help(self, args) -> str:
        return (
            "📋 <b>Apollo 命令列表</b>\n"
            "/help - 显示帮助\n"
            "/status - 策略状态\n"
            "/boot &lt;pwd&gt; - 全量重启策略\n"
            "/add &lt;name&gt; &lt;class&gt; &lt;symbol&gt; &lt;market&gt; [params_json] &lt;pwd&gt;\n"
            "/remove &lt;name&gt; &lt;pwd&gt; - 停止并移除策略\n"
            "/rollback &lt;name&gt; &lt;version&gt; &lt;pwd&gt; - 回滚参数\n"
            "/reload &lt;pwd&gt; - 手动热加载\n"
            "/cluster - 集群状态\n"
            "/stop_all &lt;pwd&gt; - 停止所有策略\n"
            "/positions - 持仓查询\n"
            "/account - 账户资金\n"
            "/shutdown &lt;pwd&gt; - 安全关闭系统"
        )

    def _cmd_status(self, args) -> str:
        if self.strategy_engine:
            return self.strategy_engine.format_status()
        tag = self.registry.tag() if self.registry else "UNKNOWN"
        return f"📊 策略状态 ({tag}):\n  无策略引擎接入"

    def _cmd_boot(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /boot &lt;password&gt;"
        pwd = args[0]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if not self.strategy_engine:
            return "❌ 策略引擎未初始化"
        result = self.strategy_engine.boot(operator="telegram:boot")
        return f"🔄 启动完成: 成功 {len(result['deployed'])}，失败 {len(result['failed'])}"

    def _cmd_add(self, args) -> str:
        if len(args) < 5:
            return "❌ 用法: /add &lt;name&gt; &lt;class&gt; &lt;symbol&gt; &lt;market&gt; &lt;params_json&gt; &lt;pwd&gt;"
        pwd = args[-1]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        name, cls, symbol, market = args[0], args[1], args[2], args[3]
        params = {}
        try:
            import json
            params = json.loads(args[4])
        except:
            return "❌ params_json 解析失败"
        if self.strategy_engine:
            ok = self.strategy_engine.add_strategy(name, cls, symbol, market, params,
                                                    source="telegram", modifier="telegram:add")
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
        except:
            return "❌ version 必须是数字"
        if self.strategy_engine:
            ok = self.strategy_engine.rollback(name, version, operator="telegram")
            return f"{'✅' if ok else '❌'} 回滚 {name} → v{version} {'成功' if ok else '失败'}"
        return "❌ 策略引擎未初始化"

    def _cmd_reload(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /reload &lt;pwd&gt;"
        pwd = args[0]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.strategy_engine:
            changed = self.strategy_engine.check_and_reload_changed(operator="telegram:reload")
            return f"🔄 热加载完成，处理: {changed if changed else '无变化'}"
        return "❌ 策略引擎未初始化"

    def _cmd_cluster(self, args) -> str:
        if not self.registry:
            return "❌ 注册表未初始化"
        status = self.registry.status()
        lines = [
            f"🖥️ 集群 [{status['cluster_id']}] 状态:",
            f"  🏷️ {status['machine_id']} [{status['instance_type']}]",
            f"  ⏱️ 运行: {status['uptime']}",
            f"  💓 模式: {status['heartbeat_mode']}",
        ]
        members = self.registry.discover()
        if members:
            lines.append(f"  ── 其他节点 ──")
            for m in members:
                lines.append(f"  🟢 {m.get('machine_id','?')} [{m.get('instance_type','?')}] up={m.get('uptime','?')}")
        else:
            lines.append("  (单机模式，无其他节点)")
        return "\n".join(lines)

    def _cmd_shutdown(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /shutdown &lt;pwd&gt;"
        pwd = args[0]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.notifier:
            self.notifier.send_shutdown_notice(reason="Telegram 远程指令")
        if self.strategy_engine:
            self.strategy_engine.stop_all()
        import threading
        def _delayed_exit():
            time.sleep(2)
            import os
            os._exit(0)
        threading.Thread(target=_delayed_exit, daemon=True).start()
        return "🛑 系统正在关闭..."

    def _cmd_stop_all(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /stop_all &lt;pwd&gt;"
        pwd = args[0]
        if pwd != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.strategy_engine:
            self.strategy_engine.stop_all()
            return "⏹️ 所有策略已停止"
        return "❌ 策略引擎未初始化"

    # ═════════════════════════════════════════
    #  ★★★ 真正的修复：直接通过网关的 trd_ctx 同步查询 ★★★
    #  （与 2.7.0 基线做法一致）
    # ═════════════════════════════════════════
    def _get_trd_ctx_from_gateway(self, gw):
        """
        从 FutuGateway 实例中提取富途交易上下文
        2.7.0 基线做法：直接使用 trd_ctx.position_list_query() / accinfo_query()
        """
        # 尝试常见属性名
        for attr in ("trd_ctx", "trade_ctx", "_trd_ctx", "trd_context"):
            ctx = getattr(gw, attr, None)
            if ctx is not None:
                return ctx
        # 如果网关暴露了 query 方法，也可以直接用
        return None

    def _cmd_positions(self, args) -> str:
        """持仓查询 - 直接通过富途 trd_ctx 同步查询（对标 2.7.0）"""
        if not self._gateways:
            return "❌ 网关注入失败，无法查询持仓"

        from futu import RET_OK
        all_positions = []
        for gw_name, gw in self._gateways.items():
            trd_ctx = self._get_trd_ctx_from_gateway(gw)
            if trd_ctx is None:
                logger.warning(f"网关 {gw_name} 无 trd_ctx，跳过")
                continue
            try:
                # ★ 关键：直接调富途同步 API（这就是 2.7.0 的做法）
                ret, data = trd_ctx.position_list_query()
                if ret == RET_OK and data is not None and not data.empty:
                    for _, row in data.iterrows():
                        qty = int(row.get("qty", 0))
                        if qty == 0:
                            continue
                        code = row.get("code", "")
                        cost = float(row.get("cost_price", 0))
                        pnl = float(row.get("pl_val", 0))
                        all_positions.append({
                            "symbol": code,
                            "qty": qty,
                            "cost": cost,
                            "pnl": pnl,
                            "gateway": gw_name,
                        })
            except Exception as e:
                logger.warning(f"网关 {gw_name} 查询持仓失败: {e}")

        if not all_positions:
            return "⚠️ 当前没有任何持仓"

        tag = self.registry.tag() if self.registry else "LOCAL"
        lines = [f"📈 <b>持仓查询</b> [{tag}]"]
        lines.append("─" * 40)
        total_pnl = 0.0
        for pos in all_positions:
            total_pnl += pos["pnl"]
            lines.append(
                f"· {pos['symbol']} ({pos['gateway']})\n"
                f"  量:{pos['qty']} 成本:{pos['cost']:.2f} 盈亏:{pos['pnl']:+.2f}"
            )
        lines.append("─" * 40)
        lines.append(f"💰 持仓总盈亏: {total_pnl:+.2f}")
        return "\n".join(lines)

    def _cmd_account(self, args) -> str:
        """账户资金查询 - 直接通过富途 trd_ctx 同步查询（对标 2.7.0）"""
        if not self._gateways:
            return "❌ 网关注入失败，无法查询资金"

        from futu import RET_OK
        all_accounts = []
        for gw_name, gw in self._gateways.items():
            trd_ctx = self._get_trd_ctx_from_gateway(gw)
            if trd_ctx is None:
                logger.warning(f"网关 {gw_name} 无 trd_ctx，跳过")
                continue
            try:
                # ★ 关键：直接调富途同步 API（这就是 2.7.0 的做法）
                ret, data = trd_ctx.accinfo_query()
                if ret == RET_OK and data is not None and not data.empty:
                    for _, row in data.iterrows():
                        total = float(row.get("total_assets", 0))
                        cash = float(row.get("cash", total))
                        all_accounts.append({
                            "gateway": gw_name,
                            "total": total,
                            "cash": cash,
                            "frozen": total - cash,
                        })
            except Exception as e:
                logger.warning(f"网关 {gw_name} 查询资金失败: {e}")

        if not all_accounts:
            return "⚠️ 未获取到账户信息"

        tag = self.registry.tag() if self.registry else "LOCAL"
        lines = [f"💰 <b>账户资金</b> [{tag}]"]
        lines.append("─" * 40)
        for acc in all_accounts:
            lines.append(
                f"🏦 {acc['gateway']}\n"
                f"  总资产: ${acc['total']:,.2f}\n"
                f"  可用:   ${acc['cash']:,.2f}\n"
                f"  冻结:   ${acc['frozen']:,.2f}"
            )
        return "\n".join(lines)