"""
Telegram confession bot — webhook backbone + button-driven UX.

Architecture overview:
  - A persistent bottom keyboard (Gossip!, Games, My Points, Change Name)
    is the main way people interact, set once via /start and re-sent after
    most actions so it's always visible.
  - "Gossip!" starts a guided ConversationHandler: type your confession ->
    pick a category via inline buttons -> posted to the channel with a
    category hashtag and a unique Post ID.
  - "Games" shows an inline menu (Wordle / Leaderboard) rather than being
    its own persistent button, since these are secondary actions.
  - Wordle guessing is session-based (stored in the DB, not a
    ConversationHandler state) so it naturally survives across days and
    bot restarts; /cancel pauses guessing without losing progress.
  - Typing without going through a button first shows a hint instead of
    silently doing nothing or misfiring into the wrong flow.

Env vars required (see .env.example):
  BOT_TOKEN                      - from @BotFather
  WEBHOOK_URL                     - public HTTPS base URL of your server
  CONFESSION_CHANNEL_ID            - numeric chat ID of the confession channel
  CONFESSION_DISCUSSION_GROUP_ID   - optional, numeric chat ID of the linked discussion group
  WEBHOOK_PATH                     - optional, defaults to /webhook/<token>
  PORT                             - optional, defaults to 8443
  WEBHOOK_SECRET                   - optional
"""

import logging
from datetime import time as dt_time, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from config import (
    ADMIN_USER_ID,
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
    CATEGORIES,
    CONFESSION_RATE_LIMIT_COUNT,
    REACTION_POINTS_MULTIPLIER,
    check_confession_rate_limit,
    get_by_post_id,
    get_confessions_ready_to_settle,
    get_my_confessions,
    get_todays_winner,
    increment_comment_count,
    init_cotd_db,
    is_tracked,
    mark_points_settled,
    track_confession,
    update_reaction_count,
)
from report_store import add_report, get_unreviewed_reports, init_reports_db, mark_reviewed
from fishing_game import (
    GAME_NAME as FISHING_GAME_NAME,
    FISH_CATALOG,
    MAX_CASTS_PER_DAY,
    REEL_WAIT_SECONDS,
    get_casts_remaining_today,
    get_tank,
    init_fishing_db,
    reel_in,
    start_cast,
)
from streak_store import get_streak, get_users_at_risk, init_streak_db, record_play
from tz_utils import SGT
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


# ---------- Persistent bottom keyboard ----------

BTN_GOSSIP = "🗣️ Gossip!"
BTN_GAMES = "🎮 Games"
BTN_MYPOINTS = "📊 My Points"
BTN_CHANGENAME = "⚙️ Change Name"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_GOSSIP, BTN_GAMES], [BTN_MYPOINTS, BTN_CHANGENAME]],
    resize_keyboard=True,
)


def format_identity_tag(identity) -> str:
    return f"{identity['avatar']} {identity['username']} #{identity['hex_id']}"


def category_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"cat:{key}")
        for key, label in CATEGORIES.items()
    ]
    # 2 per row, last one alone if odd count
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def games_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟩 Wordle", callback_data="game:wordle")],
            [InlineKeyboardButton("🎣 Fishing", callback_data="game:fishing")],
            [InlineKeyboardButton("🏆 Leaderboard", callback_data="game:leaderboard")],
        ]
    )


# ---------- /setusername conversation (unchanged, typed-command flow) ----------

AWAITING_USERNAME = 1
AWAITING_CONFESSION_TEXT = 2
AWAITING_CATEGORY = 3


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
        "Use the menu below anytime.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def setusername_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "No changes made. Use the menu below, or send /setusername anytime to try again.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ---------- Gossip! (confession) conversation ----------

async def gossip_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    identity = get_identity(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(
            "You'll need a username first. Send /setusername to set one up — "
            "it only takes a moment!"
        )
        return ConversationHandler.END

    is_allowed, minutes_left = check_confession_rate_limit(update.effective_user.id)
    if not is_allowed:
        await update.message.reply_text(
            f"You've posted {CONFESSION_RATE_LIMIT_COUNT} confessions recently — "
            f"give it about {minutes_left} more minute(s) before posting again. "
            "This keeps things from getting spammy!",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "What's on your mind? Send it as your next message.\n"
        "(Send /cancel if you change your mind.)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAITING_CONFESSION_TEXT


async def gossip_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text or ""

    is_allowed, reason = check_message(text)
    if not is_allowed:
        await update.message.reply_text(
            f"Couldn't post that: {reason}\n\nTry sending something else, or /cancel to stop."
        )
        return AWAITING_CONFESSION_TEXT

    # Stash the text in user_data until a category is picked.
    context.user_data["pending_confession_text"] = text

    await update.message.reply_text(
        "Got it! Pick a category for this post:",
        reply_markup=category_keyboard(),
    )
    return AWAITING_CATEGORY


async def gossip_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    category_key = query.data.split(":", 1)[1]
    text = context.user_data.pop("pending_confession_text", None)

    if text is None:
        # Defensive: shouldn't happen, but don't crash if it does.
        await query.edit_message_text("Something went wrong — please send /start and try again.")
        return ConversationHandler.END

    user = update.effective_user
    identity = get_identity(user.id)
    category_label = CATEGORIES.get(category_key, "")

    post_text = (
        f"{category_label}\n\n"
        f"{format_identity_tag(identity)}:\n{text}"
    )

    try:
        sent_message = await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=post_text)
    except Exception:
        logger.exception("Failed to post confession to channel")
        await query.edit_message_text("Something went wrong posting your confession. Please try again later.")
        return ConversationHandler.END

    post_id = track_confession(sent_message.message_id, user.id, text, category=category_key)

    # Edit the category-picker message to confirm, then send the keyboard
    # back as a separate message (inline-keyboard messages can't carry a
    # reply keyboard themselves).
    await query.edit_message_text(f"Posted ✅ ({category_label})\nPost ID: #{post_id}")
    await context.bot.send_message(
        chat_id=user.id,
        text="What's next?",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def gossip_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pending_confession_text", None)
    await update.message.reply_text(
        "No worries, nothing was posted. Use the menu below anytime.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ---------- Other commands & button handlers ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)

    if identity is None:
        await update.message.reply_text(
            f"Hi {user.first_name}! Welcome to the confession bot 👋\n\n"
            "Here's how this works, step by step:\n"
            "1. Set up a username with /setusername (takes 10 seconds)\n"
            "2. Once that's done, use the menu below to post confessions, "
            "play games, and check your points\n\n"
            "Let's get started — send /setusername now!"
        )
    else:
        await update.message.reply_text(
            f"Welcome back, {format_identity_tag(identity)}!\n\n"
            "Use the menu below to get started.",
            reply_markup=MAIN_KEYBOARD,
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Here's everything I can do:\n\n"
        "🆔 Getting started\n"
        "/setusername — choose or change your confession name (I'll walk you "
        "through it)\n"
        "/whoami — check what name/avatar/ID you're currently using\n\n"
        "📝 Confessions\n"
        "Tap 🗣️ Gossip! below, write your message, then pick a category. "
        "It posts to the channel under your chosen name with a Post ID.\n"
        "/myconfessions — see your own posts, best-performing first\n"
        "/report <post_id> — flag a confession for review\n\n"
        "🏆 Points & games\n"
        "Tap 🎮 Games below for Wordle, Fishing, and the leaderboard.\n"
        "/fish, /reel, /tank — cast a line, reel it in, see your collection\n"
        "📊 My Points shows your total and breakdown by game.\n\n"
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
        "Want to change it? Tap ⚙️ Change Name below, or send /setusername."
    )


async def myconfessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text(
            "You haven't set up a username yet. Send /setusername to get started!"
        )
        return

    confessions = get_my_confessions(user.id)
    if not confessions:
        await update.message.reply_text(
            "You haven't posted any confessions yet! Tap 🗣️ Gossip! to get started."
        )
        return

    lines = ["📈 Your confessions (best performing first):\n"]
    for c in confessions:
        preview = c["confession_text"][:50]
        if len(c["confession_text"]) > 50:
            preview += "..."
        category_label = CATEGORIES.get(c["category"], "") if c["category"] else ""
        lines.append(
            f"#{c['post_id']} {category_label}\n"
            f"  \"{preview}\"\n"
            f"  {c['reaction_count']} reaction(s), {c['comment_count']} comment(s)\n"
        )

    await update.message.reply_text("\n".join(lines))


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /report <post_id>\n"
            "Example: /report CYSHDZ4Y\n\n"
            "The Post ID is shown right after a confession gets posted."
        )
        return

    post_id = context.args[0].strip().upper()
    confession = get_by_post_id(post_id)

    if confession is None:
        await update.message.reply_text(
            f"Couldn't find a confession with Post ID #{post_id}. Double-check "
            "the ID and try again."
        )
        return

    add_report(post_id, update.effective_user.id)
    await update.message.reply_text(
        f"Thanks, this has been reported. We'll take a look at #{post_id}."
    )


async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    reports = get_unreviewed_reports()
    if not reports:
        await update.message.reply_text("No unreviewed reports right now. 👍")
        return

    lines = [f"📋 {len(reports)} unreviewed report(s):\n"]
    for r in reports:
        confession = get_by_post_id(r["post_id"])
        preview = confession["confession_text"][:60] if confession else "(not found)"
        lines.append(
            f"#{r['report_id']} — Post #{r['post_id']}\n"
            f"  \"{preview}{'...' if confession and len(confession['confession_text']) > 60 else ''}\"\n"
            f"  Reported by user {r['reporter_id']} at {r['reported_at']}\n"
            f"  Mark reviewed: /resolve {r['report_id']}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def resolve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /resolve <report_id>")
        return

    mark_reviewed(int(context.args[0]))
    await update.message.reply_text(f"Marked report #{context.args[0]} as reviewed.")


async def mypoints_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    streak = get_streak(user.id)
    if streak and streak["current_streak"] > 0:
        lines.append(f"\n🔥 Wordle streak: {streak['current_streak']} day(s) (best: {streak['longest_streak']})")

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


async def weekly_leaderboard_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = get_global_leaderboard(limit=20)
    text = _format_leaderboard(rows, "🏆 This Week's Leaderboard")
    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post weekly leaderboard to channel")


async def streak_warning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily DM to anyone whose streak will break if they don't play today."""
    at_risk = get_users_at_risk()
    for row in at_risk:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"🔥 Your {row['current_streak']}-day Wordle streak is about to "
                    "end! Tap 🎮 Games > Wordle before midnight to keep it alive."
                ),
            )
        except Exception:
            # User may have blocked the bot or similar — skip, don't crash the job.
            logger.warning("Failed to send streak warning to user %s", row["user_id"])


async def games_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    identity = get_identity(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(
            "You'll need a username first. Send /setusername to set one up!"
        )
        return
    await update.message.reply_text("Pick a game:", reply_markup=games_keyboard())


async def games_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "leaderboard":
        rows = get_global_leaderboard(limit=20)
        text = _format_leaderboard(rows, "🏆 Global Leaderboard (Top 20)")
        await query.edit_message_text(text)
        return

    if choice == "wordle":
        context.user_data.pop("wordle_paused", None)
        await query.edit_message_text("Loading today's Wordle...")
        await _send_wordle_status(update.effective_user.id, context, chat_id=update.effective_chat.id)
        return

    if choice == "fishing":
        await query.edit_message_text(
            "🎣 Welcome to fishing!\n\n"
            "/fish — cast your line\n"
            f"/reel — reel in after {REEL_WAIT_SECONDS}s\n"
            "/tank — see every fish you've ever caught\n\n"
            f"You get {MAX_CASTS_PER_DAY} casts a day, with a 30 minute break "
            "needed between each."
        )


async def _send_wordle_status(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    existing_result = get_result_today(user_id)
    if existing_result is not None:
        outcome = "won" if existing_result["won"] else "didn't get it"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"You've already played today's Wordle — you {outcome} in "
                f"{existing_result['guesses_used']} guess(es). Come back tomorrow "
                "for a new word!"
            ),
        )
        return

    previous_guesses = get_guesses_today(user_id)
    guesses_left = MAX_GUESSES - len(previous_guesses)
    start_session(user_id)

    if previous_guesses:
        history = "\n".join(previous_guesses)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Back to today's Wordle! Your guesses so far:\n{history}\n\n"
                f"You have {guesses_left} guess(es) left. Send your next 5-letter "
                "guess as a normal message.\n\n"
                "(Send /cancel to stop guessing for now — your progress stays saved.)"
            ),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🟩 Wordle time! Guess today's 5-letter word.\n\n"
                f"You get {MAX_GUESSES} tries. After each guess I'll show you:\n"
                "🟩 = right letter, right spot\n"
                "🟨 = right letter, wrong spot\n"
                "⬜ = not in the word\n\n"
                "Just send your guess as a normal message now!\n\n"
                "(Send /cancel to stop guessing for now — your progress stays saved.)"
            ),
        )


async def wordle_active_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /cancel while a Wordle session is active: stops "listening" for guesses
    without forfeiting progress. The user can resume via Games > Wordle.
    """
    user_id = update.effective_user.id
    if has_active_session(user_id):
        # We don't need a DB flag to "pause" — has_active_session already
        # reflects whether the game is unfinished. The actual behavior change
        # is just that plain text won't be treated as a guess until the user
        # re-opens Wordle via the Games menu. Track that with user_data.
        context.user_data["wordle_paused"] = True
        await update.message.reply_text(
            "Paused! Your guesses so far are saved. Tap 🎮 Games > Wordle anytime "
            "to pick back up.",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await update.message.reply_text("Nothing active to cancel.", reply_markup=MAIN_KEYBOARD)


async def menu_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Catches taps on the persistent bottom keyboard (which arrive as plain
    text messages matching the button labels) and routes to the right flow.
    Returns a ConversationHandler state when entering Gossip, otherwise None.
    """
    text = update.message.text

    if text == BTN_GOSSIP:
        return await gossip_entry(update, context)
    if text == BTN_GAMES:
        await games_button(update, context)
        return ConversationHandler.END
    if text == BTN_MYPOINTS:
        await mypoints_button(update, context)
        return ConversationHandler.END
    if text == BTN_CHANGENAME:
        return await setusername_entry(update, context)

    return ConversationHandler.END


async def fallback_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Plain text that doesn't match a button and isn't claimed by an active
    conversation/session. Handles Wordle guesses for users with an active,
    unpaused session; otherwise shows a hint instead of silently doing
    nothing or misfiring.
    """
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
    wordle_paused = context.user_data.get("wordle_paused", False)

    if has_active_session(user.id) and not wordle_paused and looks_like_guess:
        await _handle_wordle_guess(update, context, text)
        return

    await update.message.reply_text(
        "Use the menu below to get started — tap 🗣️ Gossip! to post, or 🎮 Games to play.",
        reply_markup=MAIN_KEYBOARD,
    )


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

        streak_result = record_play(user.id)
        lines.append(f"🔥 {streak_result['current_streak']}-day streak!")
        if streak_result["milestone_hit"]:
            bonus = streak_result["bonus_points"]
            award_points(user.id, WORDLE_GAME_NAME, bonus)
            lines.append(f"🎊 {streak_result['milestone_hit']}-day milestone! +{bonus} bonus points!")

        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)
    elif result["game_over"]:
        lines.append(f"\nOut of guesses! Today's word was: {result['answer']}")
        lines.append("Come back tomorrow for a new word!")

        streak_result = record_play(user.id)
        lines.append(f"🔥 {streak_result['current_streak']}-day streak (playing still counts!)")
        if streak_result["milestone_hit"]:
            bonus = streak_result["bonus_points"]
            award_points(user.id, WORDLE_GAME_NAME, bonus)
            lines.append(f"🎊 {streak_result['milestone_hit']}-day milestone! +{bonus} bonus points!")

        await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)
    else:
        guesses_left = MAX_GUESSES - result["guesses_used"]
        lines.append(f"\n{guesses_left} guess(es) left. Send your next guess!")
        await update.message.reply_text("\n".join(lines))


async def fish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text(
            "You'll need a username before playing. Send /setusername to set "
            "one up first!"
        )
        return

    result = start_cast(user.id)
    if not result["ok"]:
        await update.message.reply_text(result["error"])
        return

    remaining = get_casts_remaining_today(user.id)
    await update.message.reply_text(
        f"🎣 Cast! Your line is in the water...\n\n"
        f"Wait {result['wait_seconds']} seconds, then send /reel.\n"
        f"({remaining} cast(s) left today)"
    )


async def reel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text(
            "You'll need a username before playing. Send /setusername to set "
            "one up first!"
        )
        return

    result = reel_in(user.id)
    if not result["ok"]:
        await update.message.reply_text(result["error"])
        return

    new_tag = " — NEW! 🎉" if result["is_new"] else ""
    points = result["points"]
    new_total = award_points(user.id, FISHING_GAME_NAME, points)

    await update.message.reply_text(
        f"{result['emoji']} You caught: {result['name']}{new_tag}\n"
        f"Rarity: {result['rarity_label']}\n"
        f"+{points} points (total: {new_total})\n\n"
        "Use /fish to cast again, or /tank to see your collection!"
    )


async def tank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text(
            "You'll need a username before playing. Send /setusername to set "
            "one up first!"
        )
        return

    rows = get_tank(user.id)
    if not rows:
        await update.message.reply_text(
            "Your tank is empty! Use /fish to start catching."
        )
        return

    caught_keys = {row["fish_key"] for row in rows}
    counts = {row["fish_key"]: row["catch_count"] for row in rows}

    lines = [f"🐠 Your Tank ({len(caught_keys)}/{len(FISH_CATALOG)} species)\n"]
    for key, (name, emoji, rarity, points, media_file) in FISH_CATALOG.items():
        if key in caught_keys:
            lines.append(f"{emoji} {name} x{counts[key]}")
        else:
            lines.append("❓ ???")

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
    category_label = CATEGORIES.get(winner["category"], "") if winner["category"] else ""

    text = (
        "🌟 Confession of the Day 🌟\n\n"
        f"{category_label}\n\n"
        f"{tag}:\n{winner['confession_text']}\n\n"
        f"({winner['reaction_count']} reactions, {winner['comment_count']} comments)"
    )

    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post confession of the day")


async def settle_reaction_points_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic job: awards points for confessions that have crossed the
    1-hour mark since posting, based on whatever reaction count has
    accumulated by then. Each confession is settled exactly once.
    """
    ready = get_confessions_ready_to_settle()
    for confession in ready:
        points = confession["reaction_count"] * REACTION_POINTS_MULTIPLIER
        if points > 0:
            award_points(confession["user_id"], "confessions", points)
            try:
                await context.bot.send_message(
                    chat_id=confession["user_id"],
                    text=(
                        f"💬 Your confession (#{confession['post_id']}) earned "
                        f"{confession['reaction_count']} reaction(s) in its first hour — "
                        f"+{points} points!"
                    ),
                )
            except Exception:
                logger.warning("Failed to notify user %s of reaction points", confession["user_id"])
        mark_points_settled(confession["message_id"])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error %s", update, context.error, exc_info=context.error)


# ---------- App wiring ----------

def build_application() -> Application:
    init_db()
    init_points_db()
    init_wordle_db()
    init_cotd_db()
    init_fishing_db()
    init_streak_db()
    init_reports_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    setusername_conv = ConversationHandler(
        entry_points=[
            CommandHandler("setusername", setusername_entry),
            MessageHandler(filters.Regex(f"^{BTN_CHANGENAME}$"), setusername_entry),
        ],
        states={
            AWAITING_USERNAME: [
                CommandHandler("cancel", setusername_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, setusername_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", setusername_cancel)],
        name="setusername_conv",
        persistent=False,
    )

    gossip_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_GOSSIP}$"), gossip_entry)],
        states={
            AWAITING_CONFESSION_TEXT: [
                CommandHandler("cancel", gossip_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, gossip_receive_text),
            ],
            AWAITING_CATEGORY: [
                CallbackQueryHandler(gossip_category_chosen, pattern=r"^cat:"),
                CommandHandler("cancel", gossip_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", gossip_cancel)],
        name="gossip_conv",
        persistent=False,
    )

    # Order matters: conversation handlers first so they get first claim on
    # matching messages (e.g. the Gossip/Change Name button text, or a
    # message sent while a conversation is active).
    app.add_handler(setusername_conv)
    app.add_handler(gossip_conv)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("myconfessions", myconfessions_command))
    app.add_handler(CommandHandler("cancel", wordle_active_cancel))

    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_GAMES}$"), games_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_MYPOINTS}$"), mypoints_button))
    app.add_handler(CommandHandler("fish", fish_command))
    app.add_handler(CommandHandler("reel", reel_command))
    app.add_handler(CommandHandler("tank", tank_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("reports", reports_command))
    app.add_handler(CommandHandler("resolve", resolve_command))

    app.add_handler(CallbackQueryHandler(games_menu_choice, pattern=r"^game:"))

    # Catch-all for plain text: Wordle guesses or a hint, depending on state.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text_handler))

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
        time=dt_time(hour=20, minute=0, tzinfo=SGT),
        days=(0,),
    )

    # Confession of the Day — Singapore midnight, matching Wordle's daily
    # reset and everything else that's now SGT-aligned.
    app.job_queue.run_daily(
        confession_of_the_day_job,
        time=dt_time(hour=0, minute=0, tzinfo=SGT),
    )

    # Streak warning — 9pm Singapore time, a few hours before the SGT
    # midnight cutoff used by Wordle/streaks.
    app.job_queue.run_daily(
        streak_warning_job,
        time=dt_time(hour=21, minute=0, tzinfo=SGT),
    )

    # Reaction-points settlement — checks every 10 minutes for confessions
    # that have crossed the 1-hour mark and haven't been paid out yet.
    app.job_queue.run_repeating(
        settle_reaction_points_job,
        interval=timedelta(minutes=10),
        first=timedelta(seconds=30),
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
