"""
monitoring/telegram_webhook.py — Apollo-AI-Trader v2.5.0-FINAL
修复：/strategies 显示策略、/cancel all 撤单（使用 get_all_active_orders）
"""
import os
import sys
import threading
import time
import requests
from typing import Dict, Optional

from vnpy.trader.engine import MainEngine
from vnpy.trader.object import OrderData
from vnpy.trader.constant import Status
from .telegram_notifier import TelegramNotifier


class TelegramCommandListener:
    def __init__(self, main_engines: Dict[str, MainEngine], config: dict):
        self.main_engines = main_engines
        self.config = config
        self.token = config.get("telegram_token", "")
        self.chat_id = str(config.get("telegram_chat_id", config.get("chat_id", "")))
        self.notifier = TelegramNotifier(config)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0
        self.running = False
        self._thread: Optional[threading.Thread] = None

        self.command_handlers = {
            "/start": self.handle_start,
            "/help": self.handle_help,
            "/status": self.handle_status,
            "/positions": self.handle_positions,
            "/balance": self.handle_balance,
            "/strategies": self.handle_strategies,
            "/debug_buy": self.handle_debug_buy,
            "/debug_sell": self.handle_debug_sell,
            "/cancel": self.handle_cancel,
            "/shutdown": self.handle_shutdown,
        }

    def start(self):
        if self.running:
            return
        self._delete_webhook()
        self._wait_for_release()
        self._reset_offset()
        self.running = True
        self._thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._thread.start()
        print("[Telegram] 命令监听器已启动")
        self.notifier.send_message("✅ Telegram 命令监听器已启动\n输入 /help 查看命令")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _delete_webhook(self):
        try:
            resp = requests.post(
                f"{self.base_url}/deleteWebhook",
                params={"drop_pending_updates": True},
                timeout=10,
            )
            if resp.status_code == 200:
                print("[Telegram] Webhook deleted successfully")
        except Exception as e:
            print(f"[Telegram] deleteWebhook exception: {e}")

    def _wait_for_release(self):
        for i in range(12):
            try:
                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": -1, "timeout": 2},
                    timeout=5,
                )
                if resp.status_code == 200:
                    print(f"[Telegram] Connection released after {i*5}s")
                    return
                elif resp.status_code == 409:
                    print(f"[Telegram] Still conflict, waiting... ({i+1}/12)")
                    time.sleep(5)
                else:
                    return
            except Exception:
                time.sleep(5)
        print("[Telegram] Timeout waiting for release")

    def _reset_offset(self):
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={"offset": -1, "timeout": 5},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("result"):
                    self.last_update_id = data["result"][-1]["update_id"]
                    print(f"[Telegram] Reset offset to {self.last_update_id}")
                else:
                    self.last_update_id = 0
        except Exception:
            self.last_update_id = 0

    def _polling_loop(self):
        while self.running:
            try:
                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={
                        "offset": self.last_update_id + 1,
                        "timeout": 120,
                        "allowed_updates": ["message"],
                    },
                    timeout=135,
                )
                if resp.status_code == 409:
                    self._delete_webhook()
                    self._wait_for_release()
                    self._reset_offset()
                    continue
                if resp.status_code != 200:
                    time.sleep(5)
                    continue
                data = resp.json()
                if not data.get("ok"):
                    time.sleep(5)
                    continue
                for update in data.get("result", []):
                    self.last_update_id = update["update_id"]
                    message = update.get("message")
                    if message:
                        self._process_message(message)
            except requests.RequestException:
                time.sleep(10)
            except Exception:
                time.sleep(5)

    def _process_message(self, message: dict):
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()
        if not text or chat_id != self.chat_id:
            return
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []
        handler = self.command_handlers.get(command)
        if handler:
            try:
                handler(args, chat_id)
            except Exception as e:
                self.notifier.send_message(f"❌ 命令执行出错: {e}")
        else:
            self.notifier.send_message(
                f"未知命令: {command}\n输入 /help 查看可用命令"
            )

    # ---------- 辅助方法 ----------
    def _get_account_text(self, market: str, engine: MainEngine) -> str:
        try:
            accounts = engine.get_all_accounts()
            if accounts:
                acc = accounts[0]
                return f"total={acc.balance:.2f}, avail={acc.available:.2f}"
        except Exception:
            pass
        return "无账户数据"

    # ---------- 命令处理器 ----------
    def handle_start(self, args, chat_id):
        self.notifier.send_message("🤖 Apollo-AI-Trader 已就绪，输入 /help 查看命令")

    def handle_help(self, args, chat_id):
        help_text = (
            "可用命令：\n"
            "/help - 显示本帮助\n"
            "/status - 系统状态\n"
            "/positions - 当前持仓\n"
            "/balance - 账户余额\n"
            "/strategies - 列出所有策略\n"
            "/debug_buy <symbol> <vol> - 模拟买入\n"
            "/debug_sell <symbol> <vol> - 模拟卖出\n"
            "/cancel <symbol> - 撤销订单\n"
            "/cancel all - 撤销全部\n"
            "/shutdown - 远程关机"
        )
        self.notifier.send_message(help_text)

    def handle_status(self, args, chat_id):
        lines = ["系统状态："]
        for market, engine in self.main_engines.items():
            if engine is None:
                continue
            ctas = engine.get_engine("CtaStrategy")
            strat_count = len(ctas.strategies) if ctas else 0
            acc_text = self._get_account_text(market, engine)
            lines.append(f"• {market}: {strat_count} 策略, 账户: {acc_text}")
        self.notifier.send_message("\n".join(lines))

    def handle_positions(self, args, chat_id):
        lines = ["当前持仓："]
        found = False
        for market, engine in self.main_engines.items():
            if engine is None:
                continue
            try:
                for pos in engine.get_all_positions():
                    found = True
                    lines.append(f"• {pos.symbol} [{market}] vol={pos.volume} price={pos.price:.2f}")
            except Exception as e:
                lines.append(f"• [{market}] 获取持仓失败: {e}")
        if not found:
            lines.append("（无持仓）")
        self.notifier.send_message("\n".join(lines))

    def handle_balance(self, args, chat_id):
        lines = ["账户余额："]
        for market, engine in self.main_engines.items():
            if engine is None:
                continue
            acc_text = self._get_account_text(market, engine)
            lines.append(f"• {market}: {acc_text}")
        self.notifier.send_message("\n".join(lines))

    def handle_strategies(self, args, chat_id):
        """列出所有策略（直接遍历 strategies 字典）"""
        lines = ["当前策略列表："]
        for market, engine in self.main_engines.items():
            if engine is None:
                continue
            ctas = engine.get_engine("CtaStrategy")
            if ctas is None:
                continue
            for name in ctas.strategies:
                lines.append(f"• {name} [{market}]")
        if len(lines) == 1:
            lines.append("（无策略）")
        self.notifier.send_message("\n".join(lines))

    def handle_debug_buy(self, args, chat_id):
        if len(args) < 2:
            self.notifier.send_message("用法: /debug_buy <symbol> <volume>")
            return
        symbol = args[0].upper()
        try:
            vol = int(args[1])
        except ValueError:
            self.notifier.send_message("volume 需为整数")
            return
        for market, engine in self.main_engines.items():
            ctas = engine.get_engine("CtaStrategy")
            if not ctas:
                continue
            for name, strat in list(ctas.strategies.items()):
                if symbol in name.upper() or symbol in strat.vt_symbol.upper():
                    price = getattr(strat, "debug_auto_price", 100.0)
                    strat.buy(price, vol)
                    self.notifier.send_message(f"✅ {name} 模拟买入 {vol} 股 @ {price}")
                    return
        self.notifier.send_message(f"❌ 未找到 {symbol} 策略")

    def handle_debug_sell(self, args, chat_id):
        if len(args) < 2:
            self.notifier.send_message("用法: /debug_sell <symbol> <volume>")
            return
        symbol = args[0].upper()
        try:
            vol = int(args[1])
        except ValueError:
            self.notifier.send_message("volume 需为整数")
            return
        for market, engine in self.main_engines.items():
            ctas = engine.get_engine("CtaStrategy")
            if not ctas:
                continue
            for name, strat in list(ctas.strategies.items()):
                if symbol in name.upper() or symbol in strat.vt_symbol.upper():
                    price = getattr(strat, "debug_auto_price", 100.0)
                    strat.sell(price, vol)
                    self.notifier.send_message(f"✅ {name} 模拟卖出 {vol} 股 @ {price}")
                    return
        self.notifier.send_message(f"❌ 未找到 {symbol} 策略")

    def handle_cancel(self, args, chat_id):
        """全量撤单（使用 get_all_active_orders 获取活跃订单）"""
        if not args:
            self.notifier.send_message("用法: /cancel <symbol> 或 /cancel all")
            return

        target = args[0].lower()
        total_cancelled = 0
        total_failed = 0
        details = []

        for market, engine in self.main_engines.items():
            if engine is None:
                continue

            # 获取活跃订单（get_all_active_orders 内部已过滤）
            try:
                active_orders = engine.get_all_active_orders()
            except Exception as e:
                details.append(f"❌ [{market}] 获取订单失败: {str(e)[:50]}")
                total_failed += 1
                continue

            if not active_orders:
                continue

            # 筛选目标订单
            orders_to_cancel = []
            for order in active_orders:
                if target == "all" or target in order.symbol.lower():
                    orders_to_cancel.append(order)

            if not orders_to_cancel:
                continue

            # 逐个撤销
            for order in orders_to_cancel:
                try:
                    engine.cancel_order(order.vt_orderid)
                    total_cancelled += 1
                    details.append(f"✅ {order.symbol} ({market})")
                except Exception as e:
                    total_failed += 1
                    details.append(f"❌ {order.symbol} ({market}): {str(e)[:80]}")

        if total_cancelled == 0 and total_failed == 0:
            msg = "未找到匹配的活动订单"
        else:
            lines = [f"撤销结果：成功 {total_cancelled} 笔，失败 {total_failed} 笔"]
            if details:
                lines.append("详情：")
                lines.extend(details[:10])
                if len(details) > 10:
                    lines.append(f"... 共 {len(details)} 条")
            msg = "\n".join(lines)

        self.notifier.send_message(msg)

    def handle_shutdown(self, args, chat_id):
        self.notifier.send_message("⚠️ 正在强制关闭程序...")
        for market, engine in self.main_engines.items():
            if engine:
                try:
                    engine.close()
                except Exception:
                    pass
        os._exit(0)