"""
core/remote_controller.py — 远程控制器 v2.7.0
功能：处理 Telegram 命令，密码保护，策略管理
版本：v2.7.0
变更：2026-07-26 修复 /shutdown 无法彻底退出（sys.exit → os._exit）；_help 改为 HTML 格式
"""

import json
import logging
import os
import sys
import time
import subprocess

logger = logging.getLogger("RemoteController")


class RemoteController:
    def __init__(self, db=None, strategy_engine=None, notifier=None, config=None):
        self.db = db
        self.engine = strategy_engine
        self.notifier = notifier
        self.config = config or {}
        self.pwd = self.config.get("remote_password", "")
        self.main_engine = None
        self.market_switcher = None
        self._shutdown_flag = False

    # ========== 命令路由 ==========
    def handle_command(self, command: str, args: str = "") -> str:
        cmd = command.strip().lower()
        try:
            no_pwd = {
                "help": self._help,
                "status": self._status,
                "list": self._list,
                "pool": self._pool,
                "positions": self._positions,
                "account": self._account,
                "market": self._market,
            }
            if cmd in no_pwd:
                return no_pwd[cmd]()

            parts = args.split() if args else []
            if not self._check_password(parts):
                return "❌ 密码错误"

            clean = [a for a in parts if a != self.pwd]
            handlers = {
                "shutdown": lambda: self._shutdown(clean),
                "restart": lambda: self._restart(clean),
                "switch": lambda: self._switch(clean),
                "add_stock": lambda: self._add_stock(clean),
                "add": lambda: self._add_strategy(clean),
                "remove": lambda: self._remove_strategy(clean),
                "reload": lambda: self._reload(clean),
            }
            handler = handlers.get(cmd)
            if handler:
                return handler()
            return f"未知命令: /{command}。输入 /help 查看帮助。"
        except Exception as e:
            logger.error(f"[Remote] 命令执行失败: {e}")
            return f"执行失败: {e}"

    # ========== 密码校验 ==========
    def _check_password(self, parts: list) -> bool:
        if not self.pwd:
            return True
        return self.pwd in parts

    # ========== 信息查询 ==========
    def _help(self) -> str:
        return (
            "<b>Apollo AI Trader v2.7.0</b>\n"
            "━━━━━━━━━━━━━━\n"
            "<b>查询（无需密码）:</b>\n"
            "  /status - 策略状态\n"
            "  /list - 策略列表\n"
            "  /pool - 选股池\n"
            "  /positions - 持仓明细\n"
            "  /account - 账户资金\n"
            "  /market - 当前市场\n"
            "<b>管理（需密码）:</b>\n"
            "  /shutdown &lt;pwd&gt; - 关闭系统\n"
            "  /restart &lt;pwd&gt; - 重启系统\n"
            "  /switch &lt;US|HK&gt; &lt;pwd&gt; - 切换市场\n"
            "  /add_stock &lt;SYM&gt; &lt;pwd&gt; - 加股票\n"
            "  /add &lt;n&gt; &lt;cls&gt; &lt;sym&gt; &lt;mkt&gt; [json] - 加策略\n"
            "  /remove &lt;n&gt; &lt;pwd&gt; - 删策略\n"
            "  /reload &lt;pwd&gt; - 热加载"
        )

    def _status(self) -> str:
        if not self.main_engine:
            return "⚠️ 系统未就绪"
        cta = self.main_engine.get_engine("CtaStrategy")
        if not cta:
            return "⚠️ CTA引擎未就绪（策略引擎尚未挂载）"
        total = len(cta.strategies)
        running = sum(1 for s in cta.strategies.values() if s.trading)
        lines = [f"📊 策略: {running}/{total} 运行中"]
        for name, s in cta.strategies.items():
            icon = "✅" if s.trading else "⏸️"
            lines.append(f"  {icon} {name}")
        return "\n".join(lines)

    def _list(self) -> str:
        if not self.db:
            return "⚠️ 数据库未连接"
        all_s = self.db.get_all_strategies()
        if not all_s:
            return "暂无策略。"
        lines = ["策略列表:"]
        for s in all_s:
            st = "✅" if s.get("enabled") else "❌"
            lines.append(f"  {st} {s['strategy_name']} ({s.get('market','US')}) v{s.get('current_version',1)}")
        return "\n".join(lines)

    def _pool(self) -> str:
        if not self.db:
            return "⚠️ 数据库未连接"
        pool = self.db.get_pool(limit=20)
        if not pool:
            return "选股池为空。"
        lines = ["📋 最近选股池(前20):"]
        for p in pool:
            lines.append(f"  {p['stock_code']} ({p.get('market','US')}) 评分:{p.get('score',0)}")
        return "\n".join(lines)

    def _positions(self) -> str:
        if self.notifier:
            return self.notifier.handle_positions_query(self.main_engine)
        return "⚠️ 通知器未就绪"

    def _account(self) -> str:
        if self.notifier:
            return self.notifier.handle_account_query(self.main_engine)
        return "⚠️ 通知器未就绪"

    def _market(self) -> str:
        if self.market_switcher:
            s = self.market_switcher.get_status()
            return f"🌍 当前: {s['current_market']}\n📋 可用: {s['available_markets']}\n🕒 上次: {s['last_switch']}"
        return "⚠️ 市场切换器未就绪"

    # ========== 管理操作 ==========
    def _shutdown(self, args: list) -> str:
        if self.notifier:
            self.notifier.send_reply(self._chat_id(), "🔴 3秒后关闭...")
        self._shutdown_flag = True
        time.sleep(3)
        self._do_exit()
        return "正在关闭"

    def _restart(self, args: list) -> str:
        if self.notifier:
            self.notifier.send_reply(self._chat_id(), "🔄 5秒后重启...")
        time.sleep(5)
        self._do_restart()
        return "正在重启"

    def _switch(self, args: list) -> str:
        if not self.market_switcher:
            return "❌ 市场切换器未就绪"
        if not args:
            return "❌ 用法: /switch <US|HK> <pwd>"
        return self.market_switcher.manual_switch(args[0].upper())

    def _add_stock(self, args: list) -> str:
        if not args:
            return "❌ 用法: /add_stock <SYMBOL> <pwd>"
        sym = args[0].upper()
        if "." not in sym:
            sym = f"{sym}.SMART" if not sym.startswith("HK") else f"{sym}.SEHK"
        mkt = "HK" if ".SEHK" in sym else "US"
        if self.db:
            self.db.add_to_pool([{
                "stock_code": sym, "market": mkt,
                "score": 0, "reason": "manual_add", "indicators": {},
                "expires_at": "", "status": "selected"
            }])
        return f"✅ 已添加 {sym}"

    def _add_strategy(self, args: list) -> str:
        if len(args) < 4:
            return "用法: /add <name> <class> <symbol> <market> [params_json]"
        name, cls_, sym, mkt = args[0], args[1], args[2], args[3]
        params = json.loads(args[4]) if len(args) >= 5 else {}
        if "." not in sym:
            sym = f"{sym}.SEHK" if mkt.upper() == "HK" else f"{sym}.SMART"
        if self.engine:
            ok = self.engine.add_strategy(name, cls_, sym, mkt, params,
                                          source="telegram", modifier="remote")
            return f"✅ {name} 添加成功" if ok else f"❌ {name} 添加失败"
        return "❌ 策略引擎未就绪"

    def _remove_strategy(self, args: list) -> str:
        if not args:
            return "请指定策略名称"
        name = args[0]
        if self.engine:
            ok = self.engine.remove_strategy(name, operator="remote")
            return f"✅ {name} 已移除" if ok else f"❌ {name} 移除失败"
        return "❌ 策略引擎未就绪"

    def _reload(self, args: list) -> str:
        if self.engine:
            changed = self.engine.check_and_reload_changed(operator="remote")
            return f"✅ 热加载: {', '.join(changed)}" if changed else "ℹ️ 无变更"
        return "❌ 策略引擎未就绪"

    # ========== 辅助 ==========
    def _chat_id(self) -> str:
        return self.notifier.chat_id if self.notifier else ""

    def set_main_engine(self, me):
        self.main_engine = me

    def set_market_switcher(self, sw):
        self.market_switcher = sw

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_flag

    # ========== 进程控制（关键修复）==========
    def _do_exit(self):
        print("[Remote] 收到远程关闭指令，强制退出...")
        if self.main_engine:
            try:
                self.main_engine.close()
            except Exception:
                pass
        # 强制终止整个进程，无视所有线程和异常处理
        os._exit(0)

    def _do_restart(self):
        print("[Remote] 收到远程重启指令...")
        python = sys.executable
        script = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
        if self.main_engine:
            try:
                self.main_engine.close()
            except Exception:
                pass
        subprocess.Popen([python, script])
        os._exit(0)