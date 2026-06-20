"""
Telegram confession bot — webhook backbone + anonymous-but-persistent identity system.
"""

import logging
from datetime import time as dt_time

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    CONFESSION_CHANNEL_ID,
    CONFESSION_DISCUSSION_GROUP_ID,
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
from points_store import (
    award_points,
    get_global_leaderboard,
    get_global_total,
    get_per_game_breakdown,
    init_points_db,
)
from cotd_store import (
    get_todays_winner,
    increment_comment_count,
    init_cotd_db,
    is_tracked,
    track_confession,
    update_reaction_count,
)
from wordle_game import (
    GAME_NAME as WORDLE_GAME_NAME,
    MAX_GUESSES,
    get_guesses_today,
    get_result_today,
    has_active_session,
    init_wordle_db,
    start_session,
    submit_guess,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def format_identity_tag(identity) -> str:
    return f"{identity['avatar']} {identity['username']} #{identity['hex_id']}"


AWAITING_USERNAME = 1


async def setusername_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    identity = get_identity(update.effective_user.id)

    if identity is None:
        await update.message.reply_text(
            "Let's get you set up! 🎉\n\n"
            "Pick a username — this is what shows up on your confessions. "
            "Your real Telegram name is never shown, only this.\n\n"
            "Type your desired username now and send it as your next message.\n"
            "(You can send /cancel anytime to stop this.)"
        )
    else:
        await update.message.reply_text(
            f"You're currently posting as: {format_identity_tag(identity)}\n\n"
            "Type a new username and send it as your next message to change it.\n"
            "(Your hex ID stays the same even if you change your name.)\n"
            "Send /cancel if you've changed your mind."
        )
    return AWAITING_USERNAME


async def setusername_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    requested = (update.message.text or "").strip()

    is_valid, error = validate_username(requested)
    if not is_valid:
        await update.message.reply_text(
            f"That won't work: {error}\n\nTry sending a different username, "
            "or send /cancel to stop."
        )
        return AWAITING_USERNAME

    if username_taken(requested, exclude_user_id=user.id):
        await update.message.reply_text(
            "Someone's already using that username. Please try a different one, "
            "or send /cancel to stop."
        )
        return AWAITING_USERNAME

    avatar, hex_id = set_identity(user.id, requested)
    await update.message.reply_text(
        f"All set! You're now posting as: {avatar} {requested} #{hex_id}\n\n"
        "From now on, just send me any message here and I'll post it as a "
        "confession to the channel. Use /help anytime if you forget how this works.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def setusername_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "No changes made. Send /setusername anytime if you change your mind."
    )
    return ConversationHandler.END


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)

    if identity is None:
        await update.message.reply_text(
            f"Hi {user.first_name}! Welcome to the confession bot 👋\n\n"
            "Here's how this works, step by step:\n"
            "1. Set up a username with /setusername (takes 10 seconds)\n"
            "2. Once that's done, just send me any message — I'll post it to "
            "the channel under your chosen name, never your real one\n\n"
            "Let's get started — send /setusername now!"
        )
    else:
        await update.message.reply_text(
            f"Welcome back, {format_identity_tag(identity)}!\n\n"
            "Just send me any message and I'll post it as a confession.\n"
            "Other things you can do:\n"
            "/setusername — change your name\n"
            "/whoami — see your current identity\n"
            "/mypoints — check your game points\n"
            "/leaderboard — see the top players"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Here's everything I can do:\n\n"
        "🆔 Getting started\n"
        "/setusername — choose or change your confession name (I'll walk you "
        "through it)\n"
        "/whoami — check what name/avatar/ID you're currently using\n\n"
        "📝 Confessions\n"
        "Once you've set a username, just send me any normal message (not a "
        "command) and I'll post it to the channel for you. No need for a "
        "special command — anything you type gets treated as a confession.\n\n"
        "🏆 Points & games\n"
        "/wordle — play today's word game (resets daily, 6 tries)\n"
        "/mypoints — see your total points and breakdown by game\n"
        "/leaderboard — see the top 20 players\n\n"
        "Your real Telegram account is never shown publicly — only your "
        "chosen username, avatar, and a random ID number."
    )


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    identity = get_identity(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(
            "You haven't set up a username yet. Send /setusername to get started — "
            "it only takes a moment!"
        )
        return
    await update.message.reply_text(
        f"You're currently posting as: {format_identity_tag(identity)}\n\n"
        "Want to change it? Send /setusername anytime."
    )


async def mypoints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text(
            "You haven't set up a username yet. Send /setusername to get started, "
            "then come back here to check your points!"
        )
        return

    total = get_global_total(user.id)
    breakdown = get_per_game_breakdown(user.id)

    lines = [f"{format_identity_tag(identity)} — {total} points total\n"]
    if breakdown:
        lines.append("By game:")
        for row in breakdown:
            lines.append(f"  {row['game']}: {row['points']}")
    else:
        lines.append("No game points yet — play a game to start earning points!")

    await update.message.reply_text("\n".join(lines))


def _format_leaderboard(rows, title: str) -> str:
    if not rows:
        return f"{title}\n\nNo scores yet — be the first to play a game!"

    lines = [title, ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        points = row["total"] if "total" in row.keys() else row["points"]
        lines.append(f"{prefix} {row['avatar']} {row['username']} — {points} pts")
    return "\n".join(lines)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = get_global_leaderboard(limit=20)
    text = _format_leaderboard(rows, "🏆 Global Leaderboard (Top 20)")
    await update.message.reply_text(text)


async def weekly_leaderboard_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = get_global_leaderboard(limit=20)
    text = _format_leaderboard(rows, "🏆 This Week's Leaderboard")
    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post weekly leaderboard to channel")


async def wordle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text(
            "You'll need a username before playing. Send /setusername to set "
            "one up first — it only takes a moment!"
        )
        return

    existing_result = get_result_today(user.id)
    if existing_result is not None:
        outcome = "won" if existing_result["won"] else "didn't get it"
        await update.message.reply_text(
            f"You've already played today's Wordle — you {outcome} in "
            f"{existing_result['guesses_used']} guess(es). Come back tomorrow "
            "for a new word!"
        )
        return

    previous_guesses = get_guesses_today(user.id)
    guesses_left = MAX_GUESSES - len(previous_guesses)

    start_session(user.id)

    if previous_guesses:
        history = "\n".join(previous_guesses)
        await update.message.reply_text(
            f"Back to today's Wordle! Your guesses so far:\n{history}\n\n"
            f"You have {guesses_left} guess(es) left. Send your next 5-letter "
            "guess as a normal message."
        )
    else:
        await update.message.reply_text(
            "🟩 Wordle time! Guess today's 5-letter word.\n\n"
            f"You get {MAX_GUESSES} tries. After each guess I'll show you:\n"
            "🟩 = right letter, right spot\n"
            "🟨 = right letter, wrong spot\n"
            "⬜ = not in the word\n\n"
            "Just send your guess as a normal message now!"
        )


async def text_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    identity = get_identity(user.id)

    if identity is None:
        await update.message.reply_text(
            "You'll need a username first. Send /setusername to set one up — "
            "it only takes a moment!"
        )
        return

    text = (update.message.text or "").strip()

    looks_like_guess = len(text) == 5 and text.isalpha()

    if has_active_session(user.id) and looks_like_guess:
        await _handle_wordle_guess(update, context, text)
        return

    await confession_message(update, context)


async def _handle_wordle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE, guess: str) -> None:
    user = update.effective_user

    result = submit_guess(user.id, guess)

    if not result["valid"]:
        await update.message.reply_text(result["error"])
        return

    lines = [f"{guess.upper()}", result["feedback"]]

    if result["won"]:
        points = result["points_awarded"]
        new_total = award_points(user.id, WORDLE_GAME_NAME, points)
        lines.append(f"\n🎉 You got it in {result['guesses_used']} guess(es)! +{points} points")
        lines.append(f"Your wordle total is now {new_total} points.")
    elif result["game_over"]:
        lines.append(f"\nOut of guesses! Today's word was: {result['answer']}")
        lines.append("Come back tomorrow for a new word!")
    else:
        guesses_left = MAX_GUESSES - result["guesses_used"]
        lines.append(f"\n{guesses_left} guess(es) left. Send your next guess!")

    await update.message.reply_text("\n".join(lines))


async def reaction_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction_update = update.message_reaction_count
    if reaction_update is None:
        return

    message_id = reaction_update.message_id
    if not is_tracked(message_id):
        return

    total = sum(r.total_count for r in reaction_update.reactions)
    update_reaction_count(message_id, total)


async def comment_tracking_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if CONFESSION_DISCUSSION_GROUP_ID is None:
        return
    if update.effective_chat.id != CONFESSION_DISCUSSION_GROUP_ID:
        return

    message = update.message
    if message is None or message.reply_to_message is None:
        return

    replied = message.reply_to_message
    forward_origin = getattr(replied, "forward_origin", None)
    if forward_origin is None or getattr(forward_origin, "type", None) != "channel":
        return

    channel_message_id = getattr(forward_origin, "message_id", None)
    if channel_message_id is None or not is_tracked(channel_message_id):
        return

    increment_comment_count(channel_message_id)


async def confession_of_the_day_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    winner = get_todays_winner()
    if winner is None:
        return

    identity = get_identity(winner["user_id"])
    tag = format_identity_tag(identity) if identity else "Anonymous"

    text = (
        "🌟 Confession of the Day 🌟\n\n"
        f"{tag}:\n{winner['confession_text']}\n\n"
        f"({winner['reaction_count']} reactions, {winner['comment_count']} comments)"
    )

    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post confession of the day")


async def confession_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user = update.effective_user
    identity = get_identity(user.id)

    if identity is None:
        await update.message.reply_text(
            "You'll need a username before you can post a confession. "
            "Send /setusername to set one up — it only takes a moment!"
        )
        return

    text = update.message.text or ""
    is_allowed, reason = check_message(text)
    if not is_allowed:
        await update.message.reply_text(f"Couldn't post that: {reason}")
        return

    post_text = f"{format_identity_tag(identity)}:\n{text}"

    try:
        sent_message = await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=post_text)
    except Exception:
        logger.exception("Failed to post confession to channel")
        await update.message.reply_text(
            "Something went wrong posting your confession. Please try again later."
        )
        return

    track_confession(sent_message.message_id, user.id, text)
    await update.message.reply_text("Posted ✅")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error %s", update, context.error, exc_info=context.error)


def build_application() -> Application:
    init_db()
    init_points_db()
    init_wordle_db()
    init_cotd_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    setusername_conv = ConversationHandler(
        entry_points=[CommandHandler("setusername", setusername_entry)],
        states={
            AWAITING_USERNAME: [
                CommandHandler("cancel", setusername_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setusername_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", setusername_cancel)],
    )

    app.add_handler(setusername_conv)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("mypoints", mypoints_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("wordle", wordle_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_router))

    app.add_handler(
        MessageReactionHandler(
            reaction_update_handler,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_COUNT_UPDATED,
        )
    )
    if CONFESSION_DISCUSSION_GROUP_ID is not None:
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CONFESSION_DISCUSSION_GROUP_ID) & filters.REPLY,
                comment_tracking_handler,
            )
        )

    app.add_error_handler(error_handler)

    app.job_queue.run_daily(
        weekly_leaderboard_job,
        time=dt_time(hour=12, minute=0),
        days=(0,),
    )

    app.job_queue.run_daily(
        confession_of_the_day_job,
        time=dt_time(hour=0, minute=0),
    )

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
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
