# remote/remote_controller.py
import json
import logging

logger = logging.getLogger("RemoteController")

class RemoteController:
    def __init__(self, db, strategy_engine, notifier=None):
        self.db = db
        self.engine = strategy_engine
        self.notifier = notifier

    def handle_command(self, command: str, args: str = "") -> str:
        cmd = command.strip().lower()
        try:
            if cmd == "help":
                return self._help()
            elif cmd == "status":
                return self._status()
            elif cmd == "list":
                return self._list_strategies()
            elif cmd == "add":
                return self._add_strategy(args)
            elif cmd == "remove":
                return self._remove_strategy(args)
            elif cmd == "reload":
                return self._reload_all()
            elif cmd == "pool":
                return self._show_pool()
            else:
                return f"未知命令: /{command}。输入 /help 查看可用命令。"
        except Exception as e:
            logger.error(f"[Remote] 命令执行失败: {e}")
            return f"执行失败: {str(e)}"

    def _help(self) -> str:
        return (
            "可用命令:\n"
            "/status - 系统状态\n"
            "/list - 列出所有策略\n"
            "/add <name> <class> <symbol> <market> [params_json] - 添加策略\n"
            "/remove <name> - 移除策略\n"
            "/reload - 热加载所有策略\n"
            "/pool - 查看选股池"
        )

    def _status(self) -> str:
        active = self.db.get_active_strategies()
        return f"活跃策略数: {len(active)}\n选股池记录数: {len(self.db.get_pool())}"

    def _list_strategies(self) -> str:
        all_strats = self.db.get_all_strategies()
        if not all_strats:
            return "暂无策略配置。"
        lines = ["当前策略列表:"]
        for s in all_strats:
            status = "✅ 启用" if s.get("enabled") else "❌ 停用"
            lines.append(f"- {s['strategy_name']} ({s['market']}) v{s.get('current_version',1)} {status}")
        return "\n".join(lines)

    def _add_strategy(self, args: str) -> str:
        parts = args.strip().split()
        if len(parts) < 4:
            return "用法: /add <name> <class> <symbol> <market> [params_json]"
        name, cls, symbol, market = parts[0], parts[1], parts[2], parts[3]
        params = {}
        if len(parts) >= 5:
            try:
                params = json.loads(parts[4])
            except json.JSONDecodeError:
                return "参数JSON格式错误，请使用合法JSON字符串。"
        if "." not in symbol:
            if market.upper() == "HK":
                symbol = f"{symbol}.SEHK"
            else:
                symbol = f"{symbol}.SMART"
        ok = self.engine.add_strategy(name, cls, symbol, market, params, source="telegram", modifier="remote")
        if ok:
            msg = f"策略 {name} 添加成功 (vt_symbol={symbol})"
            if self.notifier:
                self.notifier.notify("INFO", msg, name)
            return msg
        else:
            return f"策略 {name} 添加失败，请检查日志。"

    def _remove_strategy(self, args: str) -> str:
        name = args.strip()
        if not name:
            return "请指定策略名称。"
        ok = self.engine.remove_strategy(name, operator="remote")
        if ok:
            msg = f"策略 {name} 已移除"
            if self.notifier:
                self.notifier.notify("INFO", msg)
            return msg
        else:
            return f"策略 {name} 移除失败，可能不存在。"

    def _reload_all(self) -> str:
        changed = self.engine.check_and_reload_changed(operator="remote")
        if changed:
            msg = f"热加载完成，更新策略: {', '.join(changed)}"
        else:
            msg = "无变更策略。"
        if self.notifier:
            self.notifier.notify("INFO", msg)
        return msg

    def _show_pool(self) -> str:
        pool = self.db.get_pool(limit=20)
        if not pool:
            return "选股池为空。"
        lines = ["最近选股池 (前20):"]
        for p in pool:
            lines.append(f"- {p['stock_code']} ({p.get('market','US')}) 评分:{p.get('score',0)}")
        return "\n".join(lines)