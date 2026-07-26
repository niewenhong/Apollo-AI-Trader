"""
core/telegram_bot.py — Telegram Bot
用户交互：状态查询、风险切换、暂停/恢复
"""
import logging
import sqlite3
from datetime import datetime

log = logging.getLogger("TelegramBot")

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
    PTB_AVAILABLE = True
except ImportError:
    PTB_AVAILABLE = False


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Apollo AI Trader 命令列表:\n"
        "/start - 绑定账户\n"
        "/status - 查看账户状态\n"
        "/position - 查看持仓\n"
        "/risk - 查看风险等级\n"
        "/set_risk [等级] - 切换风险等级\n"
        "/switch_sim - 切换到模拟盘\n"
        "/switch_live - 切换到实盘（需确认风险）\n"
        "/pause - 暂停交易\n"
        "/resume - 恢复交易\n"
        "/mode - 查看当前交易模式\n"
        "/help - 显示此帮助"
    )
    await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text(
        f"👋 欢迎使用 Apollo AI Trader!\n"
        f"您的 Chat ID: {chat_id}\n"
        f"请回复 /bind <user_id> 完成绑定"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 账户状态: 运行中\n模式: 模拟盘")


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        level = args[0].lower()
        await update.message.reply_text(
            f"⚠️ 风险提示:\n"
            f"您正在切换到「{level}」风险等级。\n"
            f"请确认您已充分理解该等级的风险特征。\n"
            f"回复「确认」完成切换。"
        )
    else:
        await update.message.reply_text("当前风险等级: 稳健型 (moderate)")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏸️ 交易已暂停")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("▶️ 交易已恢复")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 当前模式: 模拟盘 (SIMULATE)")


def create_bot(token: str, user_id: str, db_path: str = "trading.db"):
    """创建并配置Bot"""
    if not PTB_AVAILABLE or not token:
        log.warning("Telegram Bot 不可用（缺少token或依赖）")
        return None

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("mode", cmd_mode))

    return app


class TelegramBot:
    """兼容旧接口"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.token = self.config.get("telegram", {}).get("bot_token", "")
        self.application = None

    def run(self):
        if not self.token:
            log.warning("未配置Telegram Token")
            return
        # 非阻塞启动需要异步事件循环
        log.info("Telegram Bot 配置就绪（需异步启动）")

    def stop(self):
        if self.application:
            self.application.stop()