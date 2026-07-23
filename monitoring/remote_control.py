"""
remote_control.py — Apollo-AI-Tra-der v2.4.0
远程控制器（密码保护的远程管理命令）
"""
import sys
import os
import time
import subprocess
from typing import Optional

import requests


class RemoteController:
    """
    处理所有需要密码的管理命令：
    /shutdown <pwd>  - 远程关闭系统
    /restart <pwd>   - 远程重启系统
    /switch <m> <pwd> - 手动切换市场
    /add_stock <sym> <pwd> - 添加股票到选股池
    """

    def __init__(self, notifier, config: dict):
        self.notifier = notifier
        self.config = config
        self.password = config.get("remote_password", "")
        self.main_engine = None
        self.market_switcher = None
        self._shutdown_requested = False

    # ========= 命令路由 =========
    def handle_command(self, chat_id: str, user: str, text: str):
        """解析并执行命令"""
        parts = text.strip().split()
        if not parts:
            self.notifier.send_reply(chat_id, "ℹ️ 空命令")
            return

        cmd = parts[0].lower()
        args = parts[1:]

        # 不需要密码的命令
        no_pwd = {
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/account": self._cmd_account,
            "/market": self._cmd_market,
        }
        if cmd in no_pwd:
            no_pwd[cmd](chat_id)
            return

        # 需要密码的命令
        if not self._check_password(args):
            self.notifier.send_reply(chat_id, "❌ 密码错误")
            return

        # 去掉密码，解析参数
        cmd_args = [a for a in args if a != self.password]

        handlers = {
            "/shutdown": self._cmd_shutdown,
            "/restart": self._cmd_restart,
            "/switch": self._cmd_switch,
            "/add_stock": self._cmd_add_stock,
        }
        handler = handlers.get(cmd)
        if handler:
            handler(chat_id, cmd_args)
        else:
            self.notifier.send_reply(chat_id, f"ℹ️ 未知命令: {cmd}\n发送 /help 查看帮助")

    # ========= 密码验证 =========
    def _check_password(self, args: list) -> bool:
        if not self.password:
            return True  # 未设置密码时放行
        return self.password in args

    # ========= 信息查询（无需密码）=========
    def _cmd_help(self, chat_id: str):
        text = (
            "**Apollo-AI-Tra-der 命令帮助**\n"
            "━━━━━━━━━━━━━━\n"
            "**信息查询（无需密码）:**\n"
            "  /status - 查看所有策略状态\n"
            "  /positions - 查看当前持仓明细\n"
            "  /account - 查看账户资金\n"
            "  /market - 查看当前市场\n"
            "**管理操作（需要密码）:**\n"
            "  /shutdown <pwd> - 远程关闭系统\n"
            "  /restart <pwd> - 远程重启系统\n"
            "  /switch <market> <pwd> - 手动切换市场\n"
            "  /add_stock <symbol> <pwd> - 添加股票"
        )
        self.notifier.send_reply(chat_id, text)

    def _cmd_status(self, chat_id: str):
        if not self.main_engine:
            self.notifier.send_reply(chat_id, "⚠️ 系统未就绪")
            return
        cta = self.main_engine.get_engine("CtaStrategy")
        if not cta:
            self.notifier.send_reply(chat_id, "⚠️ CTA 引擎未就绪")
            return
        total = len(cta.strategies)
        running = sum(1 for s in cta.strategies.values() if s.trading)
        lines = [f"📊 策略状态: {running}/{total} 运行中"]
        for name, s in cta.strategies.items():
            icon = "✅" if s.trading else "⏸️"
            lines.append(f"  {icon} {name}")
        self.notifier.send_reply(chat_id, "\n".join(lines))

    def _cmd_positions(self, chat_id: str):
        result = self.notifier.handle_positions_query(self.main_engine)
        self.notifier.send_reply(chat_id, result)

    def _cmd_account(self, chat_id: str):
        result = self.notifier.handle_account_query(self.main_engine)
        self.notifier.send_reply(chat_id, result)

    def _cmd_market(self, chat_id: str):
        if self.market_switcher:
            status = self.market_switcher.get_status()
            text = (
                f"🌍 **当前市场**: {status['current_market']}\n"
                f"📋 可用市场: {status['available_markets']}\n"
                f"🕒 上次切换: {status['last_switch']}"
            )
        else:
            text = "⚠️ 市场切换器未就绪"
        self.notifier.send_reply(chat_id, text)

    # ========= 管理操作（需要密码）=========
    def _cmd_shutdown(self, chat_id: str, args: list):
        self.notifier.send_reply(chat_id, "🔴 系统将在 3 秒后关闭...")
        self._shutdown_requested = True
        time.sleep(3)
        self._do_shutdown()

    def _cmd_restart(self, chat_id: str, args: list):
        self.notifier.send_reply(chat_id, "🔄 系统将在 5 秒后重启...")
        time.sleep(5)
        self._do_restart()

    def _cmd_switch(self, chat_id: str, args: list):
        if not self.market_switcher:
            self.notifier.send_reply(chat_id, "❌ 市场切换器未就绪")
            return
        if not args:
            self.notifier.send_reply(chat_id, "❌ 用法: /switch <US|HK> <pwd>")
            return
        market = args[0].upper()
        result = self.market_switcher.manual_switch(market)
        self.notifier.send_reply(chat_id, result)

    def _cmd_add_stock(self, chat_id: str, args: list):
        if not args:
            self.notifier.send_reply(chat_id, "❌ 用法: /add_stock <SYMBOL> <pwd>")
            return
        symbol = args[0].upper()
        # 写入选股池
        pool_path = os.path.join(os.path.dirname(__file__), "..", "config", "ai_stock_pool.json")
        pool_path = os.path.normpath(pool_path)
        try:
            if os.path.exists(pool_path):
                with open(pool_path, "r", encoding="utf-8") as f:
                    pool = json.load(f)
            else:
                pool = []
            pool.append({
                "symbol": f"{symbol}.SMART" if "." not in symbol else symbol,
                "market": "US" if ".HK" not in symbol else "HK",
                "strategy_class": "MultiIndicatorStrategy",
                "params": {"score_threshold": 5},
            })
            with open(pool_path, "w", encoding="utf-8") as f:
                json.dump(pool, f, indent=2, ensure_ascii=False)
            self.notifier.send_reply(chat_id, f"✅ 已添加 {symbol} 到选股池")
        except Exception as e:
            self.notifier.send_reply(chat_id, f"❌ 添加失败: {e}")

    # ========= 执行操作 =========
    def _do_shutdown(self):
        print("\n[Remote] 收到远程关闭指令...")
        if self.main_engine:
            try:
                self.main_engine.close()
            except Exception:
                pass
        sys.exit(0)

    def _do_restart(self):
        print("\n[Remote] 收到远程重启指令...")
        # 重启当前 Python 进程
        python = sys.executable
        script = os.path.join(os.path.dirname(__file__), "..", "main.py")
        script = os.path.normpath(script)
        if self.main_engine:
            try:
                self.main_engine.close()
            except Exception:
                pass
        subprocess.Popen([python, script])
        sys.exit(0)

    # ========= 绑定 =========
    def set_main_engine(self, main_engine):
        self.main_engine = main_engine

    def set_market_switcher(self, switcher):
        self.market_switcher = switcher

    # ========= 状态查询 =========
    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested
