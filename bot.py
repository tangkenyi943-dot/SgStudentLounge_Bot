"""
Telegram confession bot — webhook backbone + anonymous-but-persistent identity system.
"""

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    CONFESSION_CHANNEL_ID,
    PORT,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from content_filter import check_message
from identity_store import (
    get_identity,
    init_db,
    set_identity,
    username_taken,
    validate_username,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)

    if identity is None:
        await update.message.reply_text(
            f"Hi {user.first_name}! Welcome to the confession bot 👋\n\n"
            "Before you can send confessions, pick a username. This is what "
            "shows up on your posts in the channel — people will see it, but "
            "not your real identity.\n\n"
            "Use: /setusername YourChosenName"
        )
    else:
        await update.message.reply_text(
            f"Welcome back, {identity['avatar']} {identity['username']}!\n\n"
            "Just send me any message and I'll post it as a confession.\n"
            "Use /setusername to change your name anytime, or /whoami to see "
            "your current identity."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "How this works:\n\n"
        "1. /setusername <name> — choose or change your confession identity\n"
        "2. /whoami — see your current username + avatar\n"
        "3. Send any other message — it gets posted to the channel under your "
        "chosen identity\n\n"
        "Your real Telegram account is never shown publicly — only your "
        "chosen username and avatar."
    )


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    identity = get_identity(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(
            "You haven't set a username yet. Use /setusername YourChosenName to get started."
        )
        return
    await update.message.reply_text(
        f"You're currently posting as: {identity['avatar']} {identity['username']}"
    )


async def setusername_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage: /setusername YourChosenName\n"
            "Example: /setusername MidnightOwl"
        )
        return

    requested = " ".join(context.args).strip()

    is_valid, error = validate_username(requested)
    if not is_valid:
        await update.message.reply_text(f"Can't use that username: {error}")
        return

    if username_taken(requested, exclude_user_id=user.id):
        await update.message.reply_text(
            "That username's already taken by someone else. Try another one."
        )
        return

    avatar = set_identity(user.id, requested)
    await update.message.reply_text(
        f"Done! You're now posting as: {avatar} {requested}\n\n"
        "Send me any message and I'll post it to the channel under this identity."
    )


async def confession_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    identity = get_identity(user.id)

    if identity is None:
        await update.message.reply_text(
            "You need to set a username first. Use: /setusername YourChosenName"
        )
        return

    text = update.message.text or ""
    is_allowed, reason = check_message(text)
    if not is_allowed:
        await update.message.reply_text(f"Couldn't post that: {reason}")
        return

    post_text = f"{identity['avatar']} {identity['username']}:\n{text}"

    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=post_text)
    except Exception:
        logger.exception("Failed to post confession to channel")
        await update.message.reply_text(
            "Something went wrong posting your confession. Please try again later."
        )
        return

    await update.message.reply_text("Posted ✅")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error %s", update, context.error, exc_info=context.error)


def build_application() -> Application:
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("setusername", setusername_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, confession_message))

    app.add_error_handler(error_handler)

    return app


def main() -> None:
    app = build_application()

    webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    logger.info("Starting webhook. Listening on 0.0.0.0:%s, public URL: %s", PORT, webhook_full_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_full_url,
        secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET else None,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
