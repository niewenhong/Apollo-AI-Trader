"""
monitoring/telegram_webhook.py - Apollo-AI-Trader v2.6.0
Telegram命令监听：解析消息 → 调用RemoteController
新增命令：
  /ai_select    /diagnose SYM  /optimize SYM
  /pool         /params SYM    /review SYM
"""
import logging
import re
import time
import threading
from typing import Optional

logger = logging.getLogger("TGWebhook")


class TelegramCommandListener:
    """监听Telegram消息并执行命令"""

    def __init__(self, notifier, rc, config: dict):
        self.notifier = notifier
        self.rc = rc
        self.config = config
        self.token = config.get("telegram_token","")
        self.chat_id = config.get("telegram_chat_id",0)
        self.admin_id = config.get("admin_chat_id", self.chat_id)
        self._stop = False
        self._thread = None
        self._last_update_id = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("[TG] CommandListener started")

    def stop(self):
        self._stop = True
        logger.info("[TG] CommandListener stopping")

    def _run(self):
        """轮询Telegram API获取消息"""
        if not self.token: return
        import urllib.request, json
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        while not self._stop:
            try:
                req = urllib.request.Request(
                    f"{url}?offset={self._last_update_id+1}&timeout=30")
                with urllib.request.urlopen(req, timeout=35) as r:
                    data = json.loads(r.read())
                if data.get("ok"):
                    for upd in data.get("result",[]):
                        self._last_update_id = max(self._last_update_id, upd["update_id"])
                        self._handle_update(upd)
            except Exception as e:
                logger.warning(f"[TG] poll error: {e}")
                time.sleep(5)

    def _handle_update(self, upd: dict):
        msg = upd.get("message") or upd.get("edited_message")
        if not msg: return
        chat_id = msg.get("chat",{}).get("id",0)
        text = msg.get("text","").strip()
        if chat_id != self.admin_id: return  # 只响应管理员
        if not text.startswith("/"): return
        self._dispatch(text, chat_id)

    def _dispatch(self, text: str, chat_id: int):
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd == "/start" or cmd == "/help":
                self._send(chat_id, self._help_text())
            elif cmd == "/status":
                self._send(chat_id, self.rc.get_status_text())
            elif cmd == "/positions":
                self._send(chat_id, self.rc.get_positions_text())
            elif cmd == "/pnl":
                self._send(chat_id, self.rc.get_pnl_text())
            elif cmd == "/list":
                market = args[0] if args else ""
                self._send(chat_id, self.rc.list_strategies(market))
            elif cmd == "/pause":
                self._send(chat_id, self.rc.pause_strategy(args[0],args[1]) if len(args)>=2 else "用法: /pause US name")
            elif cmd == "/resume":
                self._send(chat_id, self.rc.resume_strategy(args[0],args[1]) if len(args)>=2 else "用法: /resume US name")
            elif cmd == "/add":
                sym = args[1] if len(args)>=2 else ""
                scls = args[2] if len(args)>=3 else "MultiIndicatorStrategy"
                self._send(chat_id, self.rc.add_strategy(args[0],sym,scls) if args else "用法: /add US SYM [StrategyClass]")
            elif cmd == "/remove":
                self._send(chat_id, self.rc.remove_strategy(args[0],args[1]) if len(args)>=2 else "用法: /remove US name")
            elif cmd == "/shutdown":
                self._send(chat_id, "🔴 系统关闭中...")
                self.rc.shutdown()
            elif cmd == "/health":
                self._send(chat_id, self.rc.health_check())
            elif cmd == "/ai_select" or cmd == "/select":
                self._send(chat_id, "🤖 AI选股中...")
                result = self.rc.ai_select_now()
                self._send(chat_id, result)
            elif cmd == "/diagnose":
                sym = args[0] if args else ""
                if not sym: self._send(chat_id, "用法: /diagnose SYMBOL"); return
                self._send(chat_id, f"🩺 诊股中: {sym}...")
                self._send(chat_id, self.rc.diagnose_symbol(sym))
            elif cmd == "/optimize":
                sym = args[0] if args else ""
                if not sym: self._send(chat_id, "用法: /optimize SYMBOL"); return
                self._send(chat_id, f"⚙️ 优化中: {sym}...")
                self._send(chat_id, self.rc.optimize_symbol(sym))
            elif cmd == "/pool":
                self._send(chat_id, self.rc.show_pool())
            elif cmd == "/params":
                sym = args[0] if args else ""
                if not sym: self._send(chat_id, "用法: /params SYMBOL"); return
                self._send(chat_id, self.rc.show_params(sym))
            elif cmd == "/review":
                self._send(chat_id, "📋 审核历史功能待接入DB查询")
            elif cmd == "/buy":
                self._send(chat_id, self.rc.debug_buy(args[0],args[1]) if len(args)>=2 else "用法: /buy US SYM")
            elif cmd == "/sell":
                self._send(chat_id, self.rc.debug_sell(args[0],args[1]) if len(args)>=2 else "用法: /sell US SYM")
            elif cmd == "/cancel":
                self._send(chat_id, self.rc.debug_cancel(args[0],args[1]) if len(args)>=2 else "用法: /cancel US SYM")
            elif cmd == "/ai_on":
                self._send(chat_id, self.rc.toggle_ai(True))
            elif cmd == "/ai_off":
                self._send(chat_id, self.rc.toggle_ai(False))
            else:
                self._send(chat_id, f"未知命令: {cmd}\n输入 /help 查看帮助")
        except Exception as e:
            self._send(chat_id, f"❌ 命令执行失败: {e}")

    def _send(self, chat_id, text):
        self.notifier.send_message(chat_id, text)

    def _help_text(self) -> str:
        return """🤖 Apollo v2.6.0 命令列表:
━━━━━━━━━━━━━━━━
📊 查询:
  /status /positions /pnl /list [US|HK]
  /pool - 执行池 /params SYM - AI参数
🎮 策略:
  /pause MKT NAME /resume MKT NAME
  /add MKT SYM [Class] /remove MKT NAME
🤖 AI:
  /ai_select - 立即选股
  /diagnose SYM - 诊股
  /optimize SYM - 参数优化
🐛 调试:
  /buy MKT SYM /sell MKT SYM /cancel MKT SYM
⚙️ 系统:
  /health /shutdown /ai_on /ai_off"""
