# -*- coding: utf-8 -*-
"""
core/remote_controller.py — v2.8.8 双引擎版
修复：
  - 导入 convert_symbol_futu2vt（从 futu_gateway 模块）
  - _cmd_account / _cmd_positions 使用 gw.acc_id 而非硬编码 0
  - 增加日志输出，便于调试
"""
import logging
import time
import sys
import os
from typing import Optional, Dict, Any, List

logger = logging.getLogger("RemoteController")

# ★ 正确的 convert_symbol_futu2vt 内联定义（无需从 vnpy_futu 导入）★
# 官方 vnpy_futu 包的 EXCHANGE_VT2FUTU 映射 
def convert_symbol_futu2vt(futu_code: str):
    """
    富途代码 -> (vnpy_symbol, vnpy_exchange)
    例: 'US.AAPL'    -> ('AAPL', Exchange.SMART)
        'HK.00700'   -> ('00700', Exchange.SEHK)
        'HK_FUTURE.XXX' -> ('XXX', Exchange.HKFE)
    """
    from vnpy.trader.constant import Exchange

    if not futu_code or '.' not in futu_code:
        return futu_code, Exchange.SMART

    prefix, symbol = futu_code.split('.', 1)
    exchange_map = {
        "US": Exchange.SMART,
        "HK": Exchange.SEHK,
        "HK_FUTURE": Exchange.HKFE,
        "SH": Exchange.SSE,
        "SZ": Exchange.SZSE,
    }
    exchange = exchange_map.get(prefix, Exchange.SMART)
    return symbol, exchange


def convert_symbol_vt2futu(symbol: str, exchange) -> str:
    """
    vnpy (symbol, exchange) -> 富途代码
    例: ('AAPL', Exchange.SMART) -> 'US.AAPL'
    """
    exchange_map = {
        "SMART": "US",
        "SEHK": "HK",
        "HKFE": "HK_FUTURE",
        "SSE": "SH",
        "SZSE": "SZ",
    }
    # exchange 可能是 Exchange 枚举或字符串
    exch_key = exchange.value if hasattr(exchange, 'value') else str(exchange)
    futu_market = exchange_map.get(exch_key, "US")
    return f"{futu_market}.{symbol}"


class RemoteController:
    """Telegram 远程控制器（双引擎版 v2.8.8）"""

    def __init__(self, db=None, notifier=None, config: dict = None):
        self.db = db
        self.notifier = notifier
        self.config = config or {}
        self.main_engine_us = None
        self.main_engine_hk = None
        self.main_engine = None
        self.strategy_engine = None
        self.registry = None
        self._authorized = True
        self._gateways = {}

    def set_main_engines(self, me_us, me_hk):
        self.main_engine_us = me_us
        self.main_engine_hk = me_hk
        self.main_engine = me_us
        self._gateways = {}
        if me_us:
            self._gateways.update(getattr(me_us, 'gateways', {}))
        if me_hk:
            self._gateways.update(getattr(me_hk, 'gateways', {}))

    def set_main_engine(self, me):
        self.set_main_engines(me, me)

    def set_strategy_engine(self, engine):
        self.strategy_engine = engine

    def set_registry(self, registry):
        self.registry = registry

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
                return f"❌ 命令执行异常: {e}"
        return f"❓ 未知命令: {command}\n输入 /help 查看可用命令"

    # ══════════════════════════════════════
    #  命令实现
    # ══════════════════════════════════════
    def _cmd_help(self, args) -> str:
        return (
            "📋 <b>Apollo 命令列表</b> (v2.8.8 双引擎)\n"
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
            return self.strategy_engine.format_status()
        return f"📊 策略状态 ({self._registry_tag()}):\n  无策略引擎接入"

    def _cmd_boot(self, args) -> str:
        if len(args) < 1:
            return "❌ 用法: /boot &lt;password&gt;"
        if args[0] != self.config.get("remote_password", "admin123"):
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
        if args[0] != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.strategy_engine:
            changed = self.strategy_engine.check_and_reload_changed(operator="telegram:reload")
            return f"🔄 热加载完成，处理: {changed if changed else '无变化'}"
        return "❌ 策略引擎未初始化"

    def _cmd_cluster(self, args) -> str:
        lines = ["🖥️ <b>集群状态</b>"]
        tag = self._registry_tag()
        lines.append(f"  🏷️ 本机: {tag}")
        mode = "N/A"
        if self.registry:
            mode = getattr(self.registry, 'heartbeat_mode', 'local')
            if mode == 'N/A' and hasattr(self.registry, 'mode'):
                mode = str(self.registry.mode)
        lines.append(f"  🔒 模式: {mode}")
        inst = "N/A"
        if self.registry:
            inst = getattr(self.registry, 'instance_type', 'STANDALONE')
        lines.append(f"  🖥️ 类型: {inst}")
        members = []
        if self.registry and hasattr(self.registry, 'discover') and callable(self.registry.discover):
            try:
                members = self.registry.discover()
            except:
                members = []
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
        if args[0] != self.config.get("remote_password", "admin123"):
            return "❌ 密码错误"
        if self.notifier:
            try:
                self.notifier.send_shutdown_notice(reason="Telegram 远程指令")
            except:
                pass
        if self.strategy_engine:
            try:
                self.strategy_engine.stop_all()
            except:
                pass
        import threading, os
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

    # ══════════════════════════════════════
    #  工具函数
    # ══════════════════════════════════════
    def _registry_tag(self) -> str:
        if not self.registry:
            return "LOCAL"
        try:
            if hasattr(self.registry, 'tag'):
                attr = getattr(self.registry, 'tag')
                return attr() if callable(attr) else str(attr)
        except:
            pass
        return str(self.registry)

    def _get_trd_ctx_from_gateway(self, gw):
        for attr in ("trd_ctx", "trade_ctx", "_trd_ctx", "trd_context", "sec_trade_ctx"):
            ctx = getattr(gw, attr, None)
            if ctx is not None:
                return ctx
        return None

    def _get_acc_id_from_gateway(self, gw):
        """安全获取网关的 acc_id，默认 0"""
        return getattr(gw, 'acc_id', 0)

    def _get_env_from_gateway(self, gw):
        """安全获取网关的 trd_env"""
        env = getattr(gw, 'env', None)
        if isinstance(env, str):
            try:
                from futu import TrdEnv
                env = getattr(TrdEnv, env, TrdEnv.SIMULATE)
            except:
                env = None
        return env

    # ══════════════════════════════════════
    #  ★ 持仓查询 ★
    # ══════════════════════════════════════
    def _cmd_positions(self, args) -> str:
        if not self._gateways:
            return "❌ 网关注入失败，无法查询持仓"

        from futu import RET_OK
        all_positions = []
        for gw_name, gw in self._gateways.items():
            if gw_name == "FUTU":
                continue
            trd_ctx = self._get_trd_ctx_from_gateway(gw)
            if trd_ctx is None:
                logger.warning(f"网关 {gw_name} 无交易上下文，跳过")
                continue
            acc_id = self._get_acc_id_from_gateway(gw)
            env = self._get_env_from_gateway(gw)
            try:
                ret, data = trd_ctx.position_list_query(trd_env=env, acc_id=acc_id)
                if ret != RET_OK or data is None or data.empty:
                    logger.info(f"网关 {gw_name} 持仓查询返回空 (acc_id={acc_id})")
                    continue
                logger.info(f"网关 {gw_name} 持仓列名: {list(data.columns)}")
                for _, row in data.iterrows():
                    qty = int(row.get("qty", 0))
                    if qty == 0:
                        continue
                    symbol, exchange = convert_symbol_futu2vt(row["code"])
                    cost = float(row.get("cost_price", 0))
                    pnl = float(row.get("pl_val", 0))
                    all_positions.append({
                        "symbol": symbol,
                        "qty": qty,
                        "cost": cost,
                        "pnl": pnl,
                        "gateway": gw_name,
                    })
            except Exception as e:
                logger.warning(f"网关 {gw_name} 持仓查询失败: {e}")

        if not all_positions:
            return "⚠️ 当前没有任何持仓"
        tag = self._registry_tag()
        lines = [f"📈 <b>持仓查询</b> [{tag}] (双市场)"]
        lines.append("─" * 40)
        total_pnl = 0.0
        for pos in all_positions:
            total_pnl += pos["pnl"]
            lines.append(
                f"· {pos['symbol']} [{pos['gateway']}]\n"
                f"  量:{pos['qty']} 成本:{pos['cost']:.2f} 盈亏:{pos['pnl']:+.2f}"
            )
        lines.append("─" * 40)
        lines.append(f"💰 持仓总盈亏: {total_pnl:+.2f}")
        return "\n".join(lines)

    def _cmd_account(self, args) -> str:
        if not self._gateways:
            return "❌ 网关注入失败，无法查询资金"

        all_accounts = []
        for gw_name, gw in self._gateways.items():
            if gw_name == "FUTU":
                continue
            # 直接使用网关缓存的 acc_info（由 query_account 填充）
            acc_info = getattr(gw, 'acc_info', {})
            if acc_info:
                all_accounts.append(acc_info)
            else:
                # 兜底：直接查询
                trd_ctx = self._get_trd_ctx_from_gateway(gw)
                if trd_ctx is None:
                    continue
                acc_id = getattr(gw, 'acc_id', 0)
                env = getattr(gw, 'env', None)
                if acc_id == 0:
                    continue
                try:
                    from futu import RET_OK
                    ret, data = trd_ctx.accinfo_query(trd_env=env, acc_id=acc_id)
                    if ret != RET_OK or data is None or data.empty:
                        continue
                    for _, row in data.iterrows():
                        all_accounts.append({
                            "gateway": gw_name,
                            "total_assets": float(row.get("total_assets", 0)),
                            "cash": float(row.get("cash", 0)),
                            "market_val": float(row.get("market_val", 0)),
                            "frozen_cash": float(row.get("frozen_cash", 0)),
                            "power": float(row.get("power", 0)),
                            "currency": str(row.get("currency", "?")),
                            "market": getattr(gw, 'market', '?'),
                        })
                except Exception as e:
                    logger.warning(f"网关 {gw_name} 资金查询失败: {e}")

        if not all_accounts:
            return "⚠️ 未获取到账户信息"
        
        tag = self._registry_tag()
        lines = [f"💰 <b>账户资金</b> [{tag}] (双市场)"]
        lines.append("─" * 40)
        for acc in all_accounts:
            market_label = "🇺🇸 美股" if acc.get("market") == "US" else "🇭🇰 港股"
            lines.append(
                f"{market_label} {acc['gateway']} ({acc['currency']})\n"
                f"  总资产: ${acc['total_assets']:,.2f}\n"
                f"  现金:   ${acc['cash']:,.2f}\n"
                f"  证券市值: ${acc['market_val']:,.2f}\n"
                f"  冻结资金: ${acc['frozen_cash']:,.2f}\n"
                f"  购买力:   ${acc['power']:,.2f}"
            )
        return "\n".join(lines)