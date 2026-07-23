"""
monitoring/telegram_notifier.py - v2.6.0
纯 requests 实现 Telegram 通知 + 命令响应
资金只展示关键字段，港股模拟盘购买力兜底估算
"""
import time
import threading
from datetime import datetime
import requests


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, db=None,
                 selector=None, diagnoser=None, reporter=None):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self.db = db
        self.selector = selector
        self.diagnoser = diagnoser
        self.reporter = reporter
        self.main_us = None
        self.main_hk = None
        self._running = False
        self._thread = None
        self._last_update_id = 0

    def set_engines(self, main_us, main_hk):
        self.main_us = main_us
        self.main_hk = main_hk

    def start_polling(self):
        if not self.token or not self.chat_id:
            print("[Telegram] 未配置Token或ChatID，跳过启动")
            return
        print("[Telegram] 启动轮询监听（纯requests）...")
        self._running = True
        self._test_connection()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print("[Telegram] 命令监听已启动（/help /status /report /positions /balance）")

    def stop(self):
        self._running = False

    def _test_connection(self):
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getMe", timeout=10)
            if r.status_code == 200:
                me = r.json().get("result", {})
                print(f"[Telegram] 机器人连接成功: @{me.get('username')}")
                self.send_sync("🚀 Apollo AI Trader v2.6.0 已启动\n输入 /help 查看命令")
            else:
                print(f"[Telegram] 连接失败: {r.text}")
        except Exception as e:
            print(f"[Telegram] 连接异常: {e}")

    def _poll_loop(self):
        while self._running:
            try:
                self._process_updates()
            except Exception as e:
                print(f"[Telegram] 轮询异常: {e}")
            time.sleep(2)

    def _process_updates(self):
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self._last_update_id + 1, "timeout": 5},
                timeout=10
            )
            if r.status_code != 200:
                return
            for upd in r.json().get("result", []):
                self._last_update_id = upd["update_id"]
                msg = upd.get("message")
                if not msg:
                    continue
                cid = str(msg["chat"]["id"])
                text = (msg.get("text") or "").strip()
                if cid == self.chat_id and text.startswith("/"):
                    print(f"[Telegram] 收到命令: {text}")
                    self._handle_command(text, cid)
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"[Telegram] 处理更新异常: {e}")

    def _handle_command(self, text, chat_id):
        if text in ("/start", "/help"):
            self._reply(chat_id, self._help_text())
        elif text == "/status":
            self._reply(chat_id, self._status_text())
        elif text == "/report":
            self._reply(chat_id, "📊 正在生成日报...")
            if self.reporter:
                try:
                    self._reply(chat_id, self.reporter.generate_daily())
                except Exception as e:
                    self._reply(chat_id, f"❌ 日报失败: {e}")
            else:
                self._reply(chat_id, "❌ 日报未配置")
        elif text == "/positions":
            self._reply(chat_id, self._get_positions())
        elif text == "/balance":
            self._reply(chat_id, self._get_balance())
        else:
            self._reply(chat_id, f"❓ 未知命令: {text}\n输入 /help 查看命令")

    def _help_text(self):
        return ("🤖 <b>Apollo AI Trader 命令</b>\n\n"
                "/help - 帮助\n/status - 状态\n/report - 日报\n"
                "/positions - 持仓\n/balance - 资金")

    def _status_text(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"📡 <b>状态</b>\n🕐 {now}\n🟢 运行中\n📊 选股完成\n🌐 Webhook 运行中"

    def _get_positions(self):
        lines = ["💼 <b>当前持仓</b>"]
        try:
            for label, engine in (("美股", self.main_us), ("港股", self.main_hk)):
                if not engine:
                    continue
                lines.append(f"\n  <b>【{label}】</b>")
                got = False
                for gw_name, gateway in engine.gateways.items():
                    trade_ctx = getattr(gateway, "trade_ctx", None)
                    if trade_ctx is None:
                        continue
                    env = getattr(gateway, "env", 0)
                    try:
                        ret, data = trade_ctx.position_list_query(trd_env=env)
                        if ret == 0 and data is not None and len(data) > 0:
                            for _, row in data.iterrows():
                                code = row.get("code", "")
                                name = row.get("stock_name", "")
                                qty = row.get("qty", 0)
                                cost = float(row.get("cost_price", 0) or 0)
                                price = float(row.get("nominal_price", 0) or 0)
                                mkt = float(row.get("market_val", 0) or 0)
                                pl = float(row.get("pl_val", 0) or 0)
                                plr = float(row.get("pl_ratio", 0) or 0)
                                lines.append(
                                    f"  {code} {name} | 量 {qty} | "
                                    f"成本 {cost:.2f} | 现价 {price:.2f} | "
                                    f"市值 {mkt:,.2f} | 盈亏 {pl:,.2f} ({plr:.2f}%)"
                                )
                            got = True
                            break
                    except Exception:
                        pass
                if not got:
                    positions = engine.get_all_positions()
                    if not positions:
                        lines.append("  无持仓")
                        continue
                    for p in positions:
                        lines.append(
                            f"  {p.vt_symbol} | 量 {p.volume} | "
                            f"均价 {p.price:.2f} | 盈亏 {p.pnl:.2f}"
                        )
        except Exception as e:
            lines.append(f"  ⚠️ {e}")
        return "\n".join(lines)

    def _get_balance(self):
        lines = ["💰 <b>账户资金</b>"]
        try:
            for label, engine in (("美股", self.main_us), ("港股", self.main_hk)):
                if not engine:
                    continue
                lines.append(f"\n  <b>【{label}】</b>")
                for gw_name, gateway in engine.gateways.items():
                    trade_ctx = getattr(gateway, "trade_ctx", None)
                    if trade_ctx is None:
                        continue
                    env = getattr(gateway, "env", 0)
                    try:
                        ret, data = trade_ctx.accinfo_query(trd_env=env)
                        if ret != 0 or data is None or len(data) == 0:
                            lines.append("  ⚠️ 查询失败")
                            continue
                        row = data.iloc[0]

                        def g(*cols):
                            for c in cols:
                                if c in row.index:
                                    try:
                                        return float(row[c])
                                    except (TypeError, ValueError):
                                        return None
                            return None

                        total = g("total_assets") or 0.0
                        cash = g("cash") or 0.0
                        mkt = g("market_val") or 0.0
                        frozen = g("frozen_cash") or 0.0
                        avail = g("avl_withdrawal_cash")
                        power = g("power")
                        us_cash = g("us_cash")
                        hk_cash = g("hk_cash")

                        if label == "港股":
                            cash_disp = hk_cash if hk_cash is not None else cash
                            cash_unit = "HKD"
                        else:
                            cash_disp = us_cash if us_cash is not None else cash
                            cash_unit = "USD"

                        if power is None or power == 0:
                            if label == "港股":
                                power_disp = cash * 2
                                pnote = " (估算:现金×2)"
                            else:
                                power_disp = 0.0
                                pnote = ""
                        else:
                            power_disp = power
                            pnote = ""

                        lines.append(f"  资产净值: {total:,.2f}")
                        lines.append(f"  现金({cash_unit}): {cash_disp:,.2f}")
                        lines.append(f"  证券市值: {mkt:,.2f}")
                        lines.append(f"  冻结资金: {frozen:,.2f}")
                        lines.append(f"  可提金额: {avail if avail is not None else 0:,.2f}")
                        lines.append(f"  最大购买力: {power_disp:,.2f}{pnote}")
                        break
                    except Exception as e:
                        lines.append(f"  ⚠️ 富途查询异常: {e}")
        except Exception as e:
            lines.append(f"  ⚠️ 全局异常: {e}")
        return "\n".join(lines)

    def _reply(self, chat_id, text):
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            print(f"[Telegram] 回复失败: {e}")

    def send_sync(self, message):
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10
            )
            print(f"[Telegram] 已发送: {message[:50]}...")
        except Exception as e:
            print(f"[Telegram] 发送异常: {e}")

    def send_async(self, message):
        threading.Thread(target=self.send_sync, args=(message,), daemon=True).start()

    def send_alert(self, alert_type, message):
        emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌",
                 "success": "✅", "trade": "💰"}.get(alert_type, "📢")
        self.send_sync(f"{emoji} {message}")
