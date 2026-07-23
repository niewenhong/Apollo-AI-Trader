"""
monitoring/telegram_notifier.py - Apollo Trader v2.6.0
Telegram通知+远程控制：发送交易通知、接收控制命令
支持命令：/status, /pool, /diagnose, /optimize, /ai_confirm, /ipo
"""
import json
import asyncio
import threading
from datetime import datetime
from typing import Optional, Callable
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core.db_manager import CustomDBManager
from ai.stock_selector import AIStockSelector
from ai.stock_diagnosis import StockDiagnosis
from ai.report_generator import ReportGenerator


class TelegramNotifier:
    """Telegram通知与远程控制"""

    def __init__(self, token: str = "", chat_id: str = "",
                 db: Optional[CustomDBManager] = None,
                 selector: Optional[AIStockSelector] = None,
                 diagnoser: Optional[StockDiagnosis] = None,
                 reporter: Optional[ReportGenerator] = None):
        self.token = token
        self.chat_id = chat_id
        self.db = db
        self.selector = selector
        self.diagnoser = diagnoser
        self.reporter = reporter
        self.app = None
        self._running = False

    def set_components(self, db, selector, diagnoser, reporter):
        self.db = db
        self.selector = selector
        self.diagnoser = diagnoser
        self.reporter = reporter

    async def send_message(self, text: str, parse_mode: str = "Markdown"):
        """发送消息到指定聊天"""
        if not self.token or not self.chat_id:
            print("[Telegram] 未配置Token或ChatID")
            return
        bot = Bot(token=self.token)
        try:
            await bot.send_message(chat_id=self.chat_id, text=text,
                                   parse_mode=parse_mode)
        except Exception as e:
            print(f"[Telegram] 发送失败: {e}")

    def send_sync(self, text: str):
        """同步发送消息（供非异步上下文调用）"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.send_message(text))
        loop.close()

    # ── 命令处理 ──
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """回复系统状态"""
        msg = "*Apollo AI Trader v2.6.0*\n"
        msg += f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        if self.db:
            pool = self.db.get_top_pool(n=5)
            msg += f"AI选股池: {len(pool)} 只\n"
            if pool:
                msg += "Top 5:\n" + "\n".join(
                    [f"- {p['vt_symbol']} ({p['score']})" for p in pool[:5]]
                )
        await update.message.reply_text(msg)

    async def _cmd_pool(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看执行池"""
        if not self.db:
            await update.message.reply_text("数据库未连接")
            return
        pool = self.db.get_pool()
        if not pool:
            await update.message.reply_text("执行池为空")
            return
        msg = "*当前执行池:*\n"
        for p in pool:
            msg += f"- {p['vt_symbol']} | {p['strategy_class']} | {p['status']}\n"
        await update.message.reply_text(msg)

    async def _cmd_diagnose(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """诊股：/diagnose US.AAPL"""
        if not context.args:
            await update.message.reply_text("用法: /diagnose <代码>")
            return
        code = context.args[0]
        if not self.diagnoser:
            await update.message.reply_text("诊股模块未初始化")
            return
        try:
            result = self.diagnoser.diagnose(code)
            summary = result.get("summary", "无")
            await update.message.reply_text(f"*{code} 诊股结果*\n{summary}")
        except Exception as e:
            await update.message.reply_text(f"诊股失败: {e}")

    async def _cmd_optimize(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """优化参数：/optimize AAPL.SMART MultiIndicatorStrategy"""
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("用法: /optimize <vt_symbol> <strategy_class>")
            return
        vt_symbol, strategy_class = args[0], args[1]
        if not self.db:
            await update.message.reply_text("数据库未连接")
            return
        from ai.param_advisor import ParamAdvisor
        advisor = ParamAdvisor(self.db)
        params = advisor.suggest(vt_symbol, strategy_class)
        msg = f"*{vt_symbol} {strategy_class} 建议参数:*\n"
        msg += f"```json\n{json.dumps(params, indent=2)}\n```"
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_ai_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """AI审核确认：/ai_confirm AAPL.SMART MultiIndicatorStrategy"""
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("用法: /ai_confirm <vt_symbol> <strategy_class>")
            return
        vt_symbol, strategy_class = args[0], args[1]
        from core.decision_engine import DecisionEngine
        engine = DecisionEngine(self.db)
        ok, reason = engine.review_signal(vt_symbol, strategy_class,
                                          {"score": 80}, threshold=0.6)
        await update.message.reply_text(
            f"*审核结果:* {'✅通过' if ok else '❌拒绝'}\n原因: {reason}"
        )

    async def _cmd_ipo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看IPO信息：/ipo"""
        await update.message.reply_text(
            "IPO功能: 请使用富途API查询新股列表。\n"
            "即将支持: /ipo subscribe <code>"
        )

    def start_polling(self):
        """启动Telegram机器人轮询"""
        if not self.token:
            print("[Telegram] 未配置Token，跳过启动")
            return
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pool", self._cmd_pool))
        self.app.add_handler(CommandHandler("diagnose", self._cmd_diagnose))
        self.app.add_handler(CommandHandler("optimize", self._cmd_optimize))
        self.app.add_handler(CommandHandler("ai_confirm", self._cmd_ai_confirm))
        self.app.add_handler(CommandHandler("ipo", self._cmd_ipo))

        def run():
            self._running = True
            self.app.run_polling(allowed_updates=Update.ALL_TYPES)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        print("[Telegram] 机器人已启动")

    def stop(self):
        self._running = False
        if self.app:
            self.app.stop()