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
from pathlib import Path
from datetime import time as dt_time, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType
from telegram.helpers import escape
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
    CONFESSION_CHANNEL_USERNAME,
    CONFESSION_DISCUSSION_GROUP_ID,
    PORT,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from content_filter import check_message
from identity_store import (
    get_by_hex_id,
    get_identity,
    init_db,
    is_banned,
    list_all_identities,
    set_banned,
    set_identity,
    username_taken,
    validate_username,
)
from points_store import (
    award_points,
    get_global_leaderboard,
    get_global_total,
    get_per_game_breakdown,
    get_recent_point_events,
    get_weekly_leaderboard,
    init_points_db,
)
from cotd_store import (
    CATEGORIES,
    CONFESSION_RATE_LIMIT_COUNT,
    REACTION_POINTS_MULTIPLIER,
    check_confession_rate_limit,
    count_confessions,
    get_by_message_id,
    get_by_post_id,
    get_confessions_ready_to_settle,
    get_my_confessions,
    get_random_confession,
    get_todays_winner,
    increment_comment_count,
    init_cotd_db,
    is_tracked,
    mark_points_settled,
    track_confession,
    update_reaction_count,
)
from badges_store import BADGES, award_badge, get_user_badges, init_badges_db
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
from tz_utils import SGT, now_sgt, today_sgt
from yap_store import (
    clear_active_thread,
    get_active_thread_id,
    get_or_create_thread,
    get_other_party,
    get_thread,
    init_yap_db,
    set_active_thread,
)
from rank_system import RANK_THRESHOLDS, get_rank, get_rank_sticker_filename
from trivia_source import init_trivia_cache_db, DIFFICULTY_LABELS
from daily_trivia import (
    DIFFICULTY_POINTS as DAILY_TRIVIA_DIFFICULTY_POINTS,
    GAME_NAME as DAILY_TRIVIA_GAME_NAME,
    MAX_QUESTIONS_PER_30MIN as DAILY_TRIVIA_MAX_PER_30MIN,
    get_next_question as get_daily_trivia_question,
    init_daily_trivia_db,
    submit_answer as submit_daily_trivia_answer,
)
from trivia_1v1 import (
    DIFFICULTY_POINTS as TRIVIA_1V1_DIFFICULTY_POINTS,
    GAME_NAME as TRIVIA_1V1_GAME_NAME,
    MAX_QUESTIONS_PER_30MIN as TRIVIA_1V1_MAX_PER_30MIN,
    ROUND_DURATION_HOURS as TRIVIA_1V1_ROUND_HOURS,
    accept_challenge as accept_1v1_challenge,
    create_challenge as create_1v1_challenge,
    finalize_expired_matches,
    get_active_match_for_user,
    get_match_score,
    get_open_challenges,
    init_trivia_1v1_db,
    start_next_question as start_1v1_question,
    submit_1v1_answer,
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

RANKS_MEDIA_DIR = Path(__file__).parent / "media" / "ranks"


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


async def _try_award_badge(context: ContextTypes.DEFAULT_TYPE, user_id: int, badge_key: str) -> None:
    """Awards a badge if not already earned, and DMs the user if it's newly earned."""
    newly_awarded = award_badge(user_id, badge_key)
    if not newly_awarded:
        return
    emoji, name, description = BADGES[badge_key]
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎉 Achievement unlocked: {emoji} {name}\n{description}",
        )
    except Exception:
        logger.warning("Couldn't DM badge announcement to user %s", user_id)


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
            [InlineKeyboardButton("🧠 Daily Trivia", callback_data="game:daily_trivia")],
            [InlineKeyboardButton("⚔️ 1v1 Trivia", callback_data="game:trivia_1v1_menu")],
            [InlineKeyboardButton("🏆 All-Time Leaderboard", callback_data="game:leaderboard")],
            [InlineKeyboardButton("📅 This Week's Leaderboard", callback_data="game:weekly_leaderboard")],
            [InlineKeyboardButton("🎖️ My Rank", callback_data="game:my_rank")],
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

    if identity["banned"]:
        await update.message.reply_text(
            "Your account is currently restricted from posting confessions."
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
    bot_username = context.bot.username

    # HTML-escape user-submitted text and identity fields so confession
    # content can't break the HTML parse mode (e.g. someone typing
    # "<script>" or "&" in their confession).
    safe_text = escape(text)
    safe_username = escape(identity["username"])

    # The Gossip deep link doesn't need any payload — it just opens the
    # bot's DM, same as tapping the persistent Gossip button would.
    gossip_link = f"https://t.me/{bot_username}?start=gossip"

    def build_post_text(walkie_link: str) -> str:
        return (
            f"{category_label}\n\n"
            f"{identity['avatar']} <b>{safe_username}</b> #{identity['hex_id']}:\n"
            f"{safe_text}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Post ID: #{{POST_ID}}\n\n"
            f'Spread tea ☕ → <a href="{gossip_link}">Gossip!</a>\n'
            f'Wanna be private? → <a href="{walkie_link}">WalkieTalkie</a>\n\n'
            f"@{CONFESSION_CHANNEL_USERNAME}"
        )

    # Send once with a placeholder WalkieTalkie link (we don't know our own
    # message_id until after sending), then edit it in once we do.
    placeholder_text = build_post_text("https://t.me/" + bot_username).replace("{POST_ID}", "...")

    try:
        sent_message = await context.bot.send_message(
            chat_id=CONFESSION_CHANNEL_ID,
            text=placeholder_text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to post confession to channel")
        await query.edit_message_text("Something went wrong posting your confession. Please try again later.")
        return ConversationHandler.END

    post_id = track_confession(sent_message.message_id, user.id, text, category=category_key)

    confession_count = count_confessions(user.id)
    if confession_count == 1:
        await _try_award_badge(context, user.id, "first_steps")
    elif confession_count == 10:
        await _try_award_badge(context, user.id, "storyteller")

    streak_result = record_play(user.id)
    if streak_result["milestone_hit"]:
        bonus = streak_result["bonus_points"]
        award_points(user.id, "confessions", bonus)

    walkie_link = f"https://t.me/{bot_username}?start=walkie_{user.id}_{sent_message.message_id}"
    final_text = build_post_text(walkie_link).replace("{POST_ID}", post_id)

    try:
        await context.bot.edit_message_text(
            chat_id=CONFESSION_CHANNEL_ID,
            message_id=sent_message.message_id,
            text=final_text,
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Failed to finalize confession post %s with real links", post_id)

    streak_line = f"\n🔥 {streak_result['current_streak']}-day streak!"
    if streak_result["milestone_hit"]:
        streak_line += f" 🎊 {streak_result['milestone_hit']}-day milestone! +{bonus} bonus points!"

    await query.edit_message_text(
        f"Posted ✅ ({category_label})\nPost ID: #{post_id}{streak_line}"
    )
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

    # Deep-link payload from a confession post's text links — Telegram
    # passes whatever follows ?start= as context.args[0].
    payload = context.args[0] if context.args else None

    if payload == "gossip":
        if identity is None:
            await update.message.reply_text(
                "Got something to share? You'll need a username first. "
                "Send /setusername to set one up — it only takes a moment!"
            )
        else:
            await update.message.reply_text(
                "Got something to share? Tap 🗣️ Gossip! below to get started.",
                reply_markup=MAIN_KEYBOARD,
            )
        return

    if payload and payload.startswith("walkie_"):
        try:
            _, poster_id_str, confession_message_id_str = payload.split("_")
            poster_id = int(poster_id_str)
            confession_message_id = int(confession_message_id_str)
        except ValueError:
            await update.message.reply_text("That link looks broken — please try again from the post.")
            return

        if user.id == poster_id:
            await update.message.reply_text("That's your own confession — no need to DM yourself! 😄")
            return

        if identity is None:
            await update.message.reply_text(
                "You'll need a username before you can DM someone. "
                "Send /setusername to set one up first!"
            )
            return

        thread_id = get_or_create_thread(user.id, poster_id, confession_message_id)
        set_active_thread(user.id, thread_id)
        poster_identity = get_identity(poster_id)
        poster_tag = poster_identity["username"] if poster_identity else "them"

        await update.message.reply_text(
            f"💬 You're now DMing {poster_tag} about their confession.\n"
            "Send your message as your next text — it'll be relayed anonymously.\n"
            "(Send /cancel to step away, or /WalkieTalkie later to come back.)"
        )
        return

    if identity is None:
        await update.message.reply_text(
            f"Hi {user.first_name}! Welcome to the confession bot 👋\n\n"
            "Here's how this works, step by step:\n"
            "1. Set up a username with /setusername (takes 10 seconds)\n"
            "2. Once that's done, use the menu below to post confessions, "
            "play games, and check your points\n\n"
            "A few things worth knowing:\n"
            "🎮 We've got Wordle (daily word game) and Fishing (catch rare fish!) "
            "— both earn you points\n"
            "🏆 Check /leaderboard via the Games menu to see who's on top\n"
            "🌟 The most-reacted confession each day gets a spotlight at midnight\n\n"
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
        "/random — see a random confession from the archive\n"
        "/report <post_id> — flag a confession for review\n\n"
        "🏆 Points & games\n"
        "Tap 🎮 Games below for Wordle, Fishing, and the leaderboard.\n"
        "/fish, /reel, /tank — cast a line, reel it in, see your collection\n"
        "📊 My Points shows your total and breakdown by game.\n\n"
        "/rules — see community guidelines anytime\n\n"
        "Your real Telegram account is never shown publicly — only your "
        "chosen username, avatar, and a random ID number."
    )


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to SgStudentLounge!\n\n"
        "This is your space to confess, vent, and connect - completely anonymous, "
        "but never sketchy. Here's how it works:\n\n"
        "🗣️ Want to share something? DM @SgStudentLounge_Bot and tap Gossip! - "
        "pick a username once, then post under that name forever. Nobody sees "
        "your real identity, just your chosen name + a unique ID.\n\n"
        "📌 Categories: 😤 Rant · 💕 Love · 🆘 Help · 📚 Study · ⚖️ Debate\n\n"
        "🎮 Bored? The bot's got games - Wordle and Fishing, both with a points "
        "system and leaderboard. Check /mypoints and /leaderboard.\n\n"
        "🌟 Confession of the Day - the most-reacted post from each day gets a "
        "spotlight at midnight.\n\n"
        "🚩 See something that shouldn't be here? /report it.\n\n"
        "This is a community thing - be kind, keep it real, and have fun. 💛"
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


async def badges_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    identity = get_identity(update.effective_user.id)
    if identity is None:
        await update.message.reply_text(
            "You haven't set up a username yet. Send /setusername to get started!"
        )
        return

    earned = get_user_badges(update.effective_user.id)
    earned_keys = {b["badge_key"] for b in earned}

    lines = [f"🏆 Your achievements ({len(earned_keys)}/{len(BADGES)}):\n"]
    for key, (emoji, name, description) in BADGES.items():
        if key in earned_keys:
            lines.append(f"{emoji} {name} — {description}")
        else:
            lines.append(f"🔒 ??? — keep playing to unlock!")

    await update.message.reply_text("\n".join(lines))


async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    confession = get_random_confession()
    if confession is None:
        await update.message.reply_text(
            "No confessions posted yet! Be the first — tap 🗣️ Gossip! below."
        )
        return

    identity = get_identity(confession["user_id"])
    category_label = CATEGORIES.get(confession["category"], "") if confession["category"] else ""
    post_link = f"https://t.me/{CONFESSION_CHANNEL_USERNAME}/{confession['message_id']}"

    if identity:
        safe_username = escape(identity["username"])
        tag = f"{identity['avatar']} <b>{safe_username}</b> #{identity['hex_id']}"
    else:
        tag = "Anonymous"

    safe_text = escape(confession["confession_text"])

    text = (
        f"🎲 Random confession #{confession['post_id']}\n\n"
        f"{category_label}\n\n"
        f"{tag}:\n{safe_text}\n\n"
        f"View original: {post_link}"
    )

    await update.message.reply_text(text, parse_mode="HTML")


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


PAGE_SIZE = 10


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: full list of every user, real Telegram ID alongside their pseudonym."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    all_users = list_all_identities()
    if not all_users:
        await update.message.reply_text("No users yet.")
        return

    total_pages = (len(all_users) + PAGE_SIZE - 1) // PAGE_SIZE

    page = 1
    if context.args:
        try:
            page = int(context.args[0])
        except ValueError:
            await update.message.reply_text(f"Usage: /users <page number> (1-{total_pages})")
            return

    if page < 1 or page > total_pages:
        await update.message.reply_text(f"Page {page} doesn't exist. Valid range: 1-{total_pages}.")
        return

    start = (page - 1) * PAGE_SIZE
    page_users = all_users[start:start + PAGE_SIZE]

    lines = [f"👥 {len(all_users)} user(s) — page {page}/{total_pages}\n"]
    for u in page_users:
        ban_flag = " 🚫BANNED" if u["banned"] else ""
        lines.append(f"ID {u['user_id']} → {u['avatar']} {u['username']} #{u['hex_id']}{ban_flag}")

    if total_pages > 1:
        lines.append(f"\nSee more: /users {page + 1}" if page < total_pages else "")

    await update.message.reply_text("\n".join(lines).strip())


async def whois_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: look up a specific person by their hex ID or a confession's Post ID."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /whois <hex_id_or_post_id>")
        return

    query = context.args[0].strip().upper()

    identity = get_by_hex_id(query)
    if identity is None:
        # Not a hex ID — maybe it's a Post ID, look up the confession's author.
        confession = get_by_post_id(query)
        if confession is None:
            await update.message.reply_text(f"No match found for #{query}.")
            return
        identity = get_identity(confession["user_id"])
        if identity is None:
            await update.message.reply_text("Found the confession, but the author has no identity on record.")
            return

    ban_flag = " 🚫 BANNED" if identity["banned"] else ""
    await update.message.reply_text(
        f"Real Telegram ID: {identity['user_id']}\n"
        f"Username: {identity['avatar']} {identity['username']} #{identity['hex_id']}{ban_flag}\n"
        f"Joined: {identity['created_at']}"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: community health snapshot."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    all_users = list_all_identities()
    total_users = len(all_users)
    banned_count = sum(1 for u in all_users if u["banned"])

    await update.message.reply_text(
        f"📊 Community stats\n\n"
        f"Total users: {total_users}\n"
        f"Banned: {banned_count}\n"
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: ban a user from posting confessions, by hex ID."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /ban <hex_id>")
        return

    hex_id = context.args[0].strip().upper()
    success = set_banned(hex_id, True)
    if success:
        await update.message.reply_text(f"Banned user #{hex_id} from posting confessions.")
    else:
        await update.message.reply_text(f"No user found with hex ID #{hex_id}.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: lift a ban, by hex ID."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unban <hex_id>")
        return

    hex_id = context.args[0].strip().upper()
    success = set_banned(hex_id, False)
    if success:
        await update.message.reply_text(f"Unbanned user #{hex_id}.")
    else:
        await update.message.reply_text(f"No user found with hex ID #{hex_id}.")


async def addpoints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: manually adjust a user's points for a given game, by hex ID."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Usage: /addpoints <hex_id> <game> <amount>")
        return

    hex_id, game, amount_str = context.args[0], context.args[1], context.args[2]

    try:
        amount = int(amount_str)
    except ValueError:
        await update.message.reply_text("Amount must be a whole number (can be negative).")
        return

    identity = get_by_hex_id(hex_id)
    if identity is None:
        await update.message.reply_text(f"No user found with hex ID #{hex_id}.")
        return

    new_total = award_points(identity["user_id"], game, amount)
    await update.message.reply_text(
        f"Adjusted {identity['username']}'s {game} points by {amount:+d}. "
        f"New {game} total: {new_total}."
    )


async def pointlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: recent point-earning events across everyone, newest first."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("This command is admin-only.")
        return

    # Fetch a generous pool to paginate through in-memory. If you ever need
    # to look further back than this, raise the limit here.
    events = get_recent_point_events(limit=200)
    if not events:
        await update.message.reply_text("No point events logged yet.")
        return

    total_pages = (len(events) + PAGE_SIZE - 1) // PAGE_SIZE

    page = 1
    if context.args:
        try:
            page = int(context.args[0])
        except ValueError:
            await update.message.reply_text(f"Usage: /pointlog <page number> (1-{total_pages})")
            return

    if page < 1 or page > total_pages:
        await update.message.reply_text(f"Page {page} doesn't exist. Valid range: 1-{total_pages}.")
        return

    start = (page - 1) * PAGE_SIZE
    page_events = events[start:start + PAGE_SIZE]

    lines = [f"🧾 Point events (newest first) — page {page}/{total_pages}\n"]
    for e in page_events:
        who = f"{e['avatar']} {e['username']} #{e['hex_id']}" if e["username"] else f"user {e['user_id']}"
        lines.append(f"{e['occurred_at']} — {who}: {e['points']:+d} ({e['game']})")

    if page < total_pages:
        lines.append(f"\nSee more: /pointlog {page + 1}")

    await update.message.reply_text("\n".join(lines))


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
        lines.append(f"\n🔥 Streak: {streak['current_streak']} day(s) (best: {streak['longest_streak']})")

    rank = get_rank(total)
    lines.append(f"\n{rank['label']}")
    if rank["points_to_next"] is not None:
        lines.append(f"({rank['points_to_next']} points to next rank)")

    await update.message.reply_text("\n".join(lines))


async def _send_rank(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """
    Shared helper: sends the user's current rank as a small sticker
    (matching the tier), followed by a separate text message with the
    actual rank/points details. Stickers can't carry a caption — that's
    why this is two messages instead of one photo-with-caption. Used by
    both /rank (a real command) and the Games-menu "My Rank" button (a
    callback query) — chat_id passed explicitly so it works correctly
    from either context, same pattern as the trivia helper.
    """
    identity = get_identity(user_id)
    if identity is None:
        await context.bot.send_message(chat_id=chat_id, text="You haven't set up a username yet. Send /setusername to get started!")
        return

    total = get_global_total(user_id)
    rank = get_rank(total)

    sticker_filename = get_rank_sticker_filename(rank["tier"])
    sticker_path = RANKS_MEDIA_DIR / sticker_filename

    try:
        with open(sticker_path, "rb") as f:
            await context.bot.send_sticker(chat_id=chat_id, sticker=f)
    except FileNotFoundError:
        logger.warning("Rank sticker not found: %s", sticker_path)
    except Exception:
        logger.exception("Failed to send rank sticker, continuing with text only")

    text = f"{format_identity_tag(identity)}\n\n{rank['label']}\nTotal points: {total}"
    if rank["points_to_next"] is not None:
        text += f"\n{rank['points_to_next']} points to {_next_rank_label(rank)}"

    await context.bot.send_message(chat_id=chat_id, text=text)


def _next_rank_label(rank: dict) -> str:
    """Small helper to phrase the 'next rank' line readably."""
    for tier, sublevel, min_points in RANK_THRESHOLDS:
        if min_points == rank["next_threshold"]:
            return f"{tier} {sublevel}"
    return "the next rank"


async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_rank(update.effective_user.id, context, chat_id=update.effective_chat.id)


def _format_leaderboard(rows, title: str) -> str:
    if not rows:
        return f"{title}\n\nNo scores yet — be the first to play a game!"

    lines = [title, ""]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        has_breakdown = "wordle_points" in row.keys()
        total = row["total"] if "total" in row.keys() else row["points"]
        rank = get_rank(total)
        if has_breakdown:
            lines.append(
                f"{prefix} {row['avatar']} {row['username']} — {rank['label']}\n"
                f"     Total: {total} | 🟩 Wordle: {row['wordle_points']} | 🎣 Fishing: {row['fishing_points']}"
            )
        else:
            lines.append(f"{prefix} {row['avatar']} {row['username']} — {rank['label']} ({total} pts)")
    return "\n".join(lines)


async def weekly_leaderboard_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = get_weekly_leaderboard(limit=20)
    text = _format_leaderboard(rows, "🏆 This Week's Top Players")
    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post weekly leaderboard to channel")


async def periodic_leaderboard_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Posts this week's leaderboard to the channel once daily, at midnight SGT."""
    rows = get_weekly_leaderboard(limit=20)
    text = _format_leaderboard(rows, "📅 This Week's Leaderboard")
    try:
        await context.bot.send_message(chat_id=CONFESSION_CHANNEL_ID, text=text)
    except Exception:
        logger.exception("Failed to post periodic leaderboard to channel")


async def streak_warning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily DM to anyone whose streak will break if they don't do something today."""
    at_risk = get_users_at_risk()
    for row in at_risk:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"🔥 Your {row['current_streak']}-day streak is about to end! "
                    "Play Wordle, go fishing, or post a confession before midnight "
                    "to keep it alive."
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

    if choice == "daily_trivia":
        await query.edit_message_text("Loading today's trivia...")
        await _send_daily_trivia_question(update.effective_user.id, context, chat_id=update.effective_chat.id)
        return

    if choice == "trivia_1v1_menu":
        await query.edit_message_text(
            f"⚔️ 1v1 Trivia\n\n"
            f"Challenge runs for {TRIVIA_1V1_ROUND_HOURS} hours. Answer up to "
            f"{TRIVIA_1V1_MAX_PER_30MIN} questions per 30 minutes — most weighted points wins!\n"
            f"🟢 Easy = {TRIVIA_1V1_DIFFICULTY_POINTS['easy']}pts · 🟡 Medium = {TRIVIA_1V1_DIFFICULTY_POINTS['medium']}pts · 🔴 Hard = {TRIVIA_1V1_DIFFICULTY_POINTS['hard']}pts\n\n"
            f"Send /trivia1v1 to start or check your match."
        )
        return

    if choice == "my_rank":
        await query.edit_message_text("Loading your rank...")
        await _send_rank(update.effective_user.id, context, chat_id=update.effective_chat.id)
        return

    if choice == "leaderboard":
        rows = get_global_leaderboard(limit=20)
        text = _format_leaderboard(rows, "🏆 All-Time Leaderboard (Top 20)")
        await query.edit_message_text(text)
        return

    if choice == "weekly_leaderboard":
        rows = get_weekly_leaderboard(limit=20)
        text = _format_leaderboard(rows, "📅 This Week's Leaderboard (Top 20)")
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


async def general_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Unified /cancel: checks for an active YAP thread first (since that's
    typically the more time-sensitive thing to step away from), then an
    active Wordle session, otherwise reports nothing to cancel.
    """
    user_id = update.effective_user.id

    if get_active_thread_id(user_id) is not None:
        clear_active_thread(user_id)
        await update.message.reply_text(
            "Stepped away from that conversation. Send /WalkieTalkie anytime to "
            "pick it back up.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

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
        return

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

    active_thread_id = get_active_thread_id(user.id)
    if active_thread_id is not None:
        await _relay_yap_message(update, context, active_thread_id, text)
        return

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

        if result["guesses_used"] == 1:
            await _try_award_badge(context, user.id, "word_wizard")

        streak_result = record_play(user.id)
        lines.append(f"🔥 {streak_result['current_streak']}-day streak!")
        if streak_result["milestone_hit"]:
            bonus = streak_result["bonus_points"]
            award_points(user.id, WORDLE_GAME_NAME, bonus)
            lines.append(f"🎊 {streak_result['milestone_hit']}-day milestone! +{bonus} bonus points!")

        if streak_result["current_streak"] >= 5:
            await _try_award_badge(context, user.id, "dedicated")
        if streak_result["current_streak"] >= 50:
            await _try_award_badge(context, user.id, "centurion")

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


async def _send_daily_trivia_question(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """
    Shared helper: sends the player's next trivia question (or a
    rate-limit message) to chat_id. Used by both /trivia (a real
    command, has update.message) and the Games-menu button (a callback
    query, where update.message is None — chat_id is passed explicitly
    instead so this works correctly from either context).
    """
    identity = get_identity(user_id)
    if identity is None:
        await context.bot.send_message(chat_id=chat_id, text="You'll need a username first. Send /setusername to set one up!")
        return

    result = get_daily_trivia_question(user_id, now_sgt())
    if not result["ok"]:
        await context.bot.send_message(chat_id=chat_id, text=result["error"])
        return

    difficulty_label = DIFFICULTY_LABELS.get(result["difficulty"], result["difficulty"])
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(opt, callback_data=f"trivia_answer:{opt}")] for opt in result["options"]]
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🧠 {difficulty_label}\n\n{result['question']}",
        reply_markup=keyboard,
    )


async def trivia_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_daily_trivia_question(update.effective_user.id, context, chat_id=update.effective_chat.id)


async def trivia_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chosen = query.data.split(":", 1)[1]
    user = update.effective_user

    result = submit_daily_trivia_answer(user.id, chosen, now_sgt())
    if not result["ok"]:
        await query.edit_message_text(result["error"])
        return

    if result["correct"]:
        new_total = award_points(user.id, DAILY_TRIVIA_GAME_NAME, result["points_awarded"])
        streak_result = record_play(user.id)
        text = (
            f"✅ Correct! +{result['points_awarded']} points (total: {new_total})\n"
            f"🔥 {streak_result['current_streak']}-day streak!"
        )
        if streak_result["milestone_hit"]:
            bonus = streak_result["bonus_points"]
            award_points(user.id, DAILY_TRIVIA_GAME_NAME, bonus)
            text += f"\n🎊 {streak_result['milestone_hit']}-day milestone! +{bonus} bonus points!"
        text += "\n\nSend /trivia for your next question!"
    else:
        text = f"❌ Not quite — the correct answer was: {result['correct_answer']}\n\nSend /trivia to keep going!"

    await query.edit_message_text(text)


async def trivia1v1_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /trivia1v1 — shows current match status if in one, otherwise offers to
    start a new challenge (random matchmaking or join an open one).
    """
    user = update.effective_user
    identity = get_identity(user.id)
    if identity is None:
        await update.message.reply_text("You'll need a username first. Send /setusername to set one up!")
        return

    finalize_expired_matches()  # lazily settle any matches that just expired

    active = get_active_match_for_user(user.id)
    if active is not None:
        opponent_id = active["player_b_id"] if active["player_a_id"] == user.id else active["player_a_id"]
        opponent_identity = get_identity(opponent_id)
        opponent_name = opponent_identity["username"] if opponent_identity else "your opponent"

        my_score = get_match_score(active["match_id"], user.id)
        their_score = get_match_score(active["match_id"], opponent_id)

        await update.message.reply_text(
            f"⚔️ Active match vs {opponent_name}\n\n"
            f"Your score: {my_score}\nTheir score: {their_score}\n\n"
            f"Ends: {active['ends_at']}\n\n"
            f"Send /trivia1v1answer to get your next question "
            f"(up to {TRIVIA_1V1_MAX_PER_30MIN} per 30 minutes)."
        )
        return

    open_matches = get_open_challenges(exclude_user_id=user.id)
    if open_matches:
        lines = ["⚔️ Open challenges waiting for an opponent:\n"]
        keyboard_rows = []
        for m in open_matches:
            challenger = get_identity(m["player_a_id"])
            name = challenger["username"] if challenger else "Someone"
            lines.append(f"- vs {name}")
            keyboard_rows.append([InlineKeyboardButton(f"Join vs {name}", callback_data=f"trivia1v1_join:{m['match_id']}")])
        keyboard_rows.append([InlineKeyboardButton("🎲 Start a new open challenge instead", callback_data="trivia1v1_new")])
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard_rows))
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎲 Start an open challenge (random matchmaking)", callback_data="trivia1v1_new")]]
    )
    await update.message.reply_text(
        f"⚔️ 1v1 Trivia\n\n"
        f"Challenge runs for {TRIVIA_1V1_ROUND_HOURS} hours. Answer up to "
        f"{TRIVIA_1V1_MAX_PER_30MIN} questions per 30 minutes — most weighted points wins!\n"
        f"🟢 Easy = {TRIVIA_1V1_DIFFICULTY_POINTS['easy']}pts · 🟡 Medium = {TRIVIA_1V1_DIFFICULTY_POINTS['medium']}pts · 🔴 Hard = {TRIVIA_1V1_DIFFICULTY_POINTS['hard']}pts",
        reply_markup=keyboard,
    )


async def trivia1v1_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    if query.data == "trivia1v1_new":
        match_id = create_1v1_challenge(user.id, None)
        await query.edit_message_text(
            "🎲 Open challenge created! Waiting for someone to join — "
            "anyone who runs /trivia1v1 will see it. You'll start once they accept."
        )
        return

    if query.data.startswith("trivia1v1_join:"):
        match_id = int(query.data.split(":", 1)[1])
        success = accept_1v1_challenge(match_id, user.id)
        if not success:
            await query.edit_message_text("That challenge isn't available anymore — try /trivia1v1 again.")
            return
        await query.edit_message_text(
            "⚔️ Challenge accepted! Send /trivia1v1answer to get your first question."
        )


async def trivia1v1_answer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetches (or re-shows) the player's current pending question in their active match."""
    user = update.effective_user
    active = get_active_match_for_user(user.id)
    if active is None:
        await update.message.reply_text("You don't have an active 1v1 match. Send /trivia1v1 to start one!")
        return

    result = start_1v1_question(active["match_id"], user.id)
    if not result["ok"]:
        await update.message.reply_text(result["error"])
        return

    difficulty_label = DIFFICULTY_LABELS.get(result["difficulty"], result["difficulty"])
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(opt, callback_data=f"trivia1v1_answer:{active['match_id']}:{opt}")] for opt in result["options"]]
    )
    await update.message.reply_text(
        f"⚔️ {difficulty_label}\n\n{result['question']}",
        reply_markup=keyboard,
    )


async def trivia1v1_answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, match_id_str, chosen = query.data.split(":", 2)
    match_id = int(match_id_str)
    user = update.effective_user

    result = submit_1v1_answer(match_id, user.id, chosen)
    if not result["ok"]:
        await query.edit_message_text(result["error"])
        return

    if result["correct"]:
        award_points(user.id, TRIVIA_1V1_GAME_NAME, result["points_awarded"])
        text = f"✅ Correct! +{result['points_awarded']} points this round.\n\nSend /trivia1v1answer for your next question."
    else:
        text = f"❌ Not quite — the answer was: {result['correct_answer']}\n\nSend /trivia1v1answer to keep going."

    await query.edit_message_text(text)


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

    if result["is_new"]:
        tank = get_tank(user.id)
        if len(tank) == 1:
            await _try_award_badge(context, user.id, "first_catch")
        if len(tank) == len(FISH_CATALOG):
            await _try_award_badge(context, user.id, "collector")

    if result["rarity"] == "secret":
        await _try_award_badge(context, user.id, "jackpot")

    streak_result = record_play(user.id)
    streak_line = f"\n🔥 {streak_result['current_streak']}-day streak!"
    fishing_bonus_note = ""
    if streak_result["milestone_hit"]:
        bonus = streak_result["bonus_points"]
        award_points(user.id, FISHING_GAME_NAME, bonus)
        fishing_bonus_note = f" 🎊 {streak_result['milestone_hit']}-day milestone! +{bonus} bonus points!"

    await update.message.reply_text(
        f"{result['emoji']} You caught: {result['name']}{new_tag}\n"
        f"Rarity: {result['rarity_label']}\n"
        f"+{points} points (total: {new_total})\n"
        f"{streak_line}{fishing_bonus_note}\n\n"
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


async def gossip_from_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fired when someone taps the 'Gossip' button on a confession post in the
    channel. Conversation state is scoped per-chat, and this button lives
    in the channel chat, not the user's private DM with the bot — so rather
    than trying to inject them into the existing ConversationHandler (which
    would track state against the channel, not their DM), we just nudge
    them to continue in their own chat with the bot, where the persistent
    keyboard and the real Gossip flow live.
    """
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    identity = get_identity(user_id)

    if identity is None:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "Inspired to share something? You'll need a username first. "
                    "Send /setusername to set one up — it only takes a moment!"
                ),
            )
        except Exception:
            logger.warning("Couldn't DM user %s — they may not have started the bot yet", user_id)
        return

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="Got something to share? Tap 🗣️ Gossip! below to get started.",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception:
        logger.warning("Couldn't DM user %s — they may not have started the bot yet", user_id)


async def yap_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fired when someone taps 'DM <username>' under a confession in the channel."""
    query = update.callback_query
    await query.answer()

    _, poster_id_str, confession_message_id_str = query.data.split(":")
    poster_id = int(poster_id_str)
    confession_message_id = int(confession_message_id_str)
    reader_id = update.effective_user.id

    if reader_id == poster_id:
        await context.bot.send_message(
            chat_id=reader_id,
            text="That's your own confession — no need to DM yourself! 😄",
        )
        return

    reader_identity = get_identity(reader_id)
    if reader_identity is None:
        await context.bot.send_message(
            chat_id=reader_id,
            text=(
                "You'll need a username before you can DM someone. "
                "Send /setusername to set one up first!"
            ),
        )
        return

    thread_id = get_or_create_thread(reader_id, poster_id, confession_message_id)
    set_active_thread(reader_id, thread_id)

    poster_identity = get_identity(poster_id)
    poster_tag = poster_identity["username"] if poster_identity else "them"

    await context.bot.send_message(
        chat_id=reader_id,
        text=(
            f"💬 You're now DMing {poster_tag} about their confession.\n"
            "Send your message as your next text — it'll be relayed anonymously.\n"
            "(Send /cancel to step away, or /WalkieTalkie later to come back.)"
        ),
    )


async def yap_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fired when someone taps 'Reply' under a relayed YAP message."""
    query = update.callback_query
    await query.answer()

    thread_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id

    thread = get_thread(thread_id)
    if thread is None:
        await context.bot.send_message(chat_id=user_id, text="This conversation isn't available anymore.")
        return

    set_active_thread(user_id, thread_id)
    await context.bot.send_message(
        chat_id=user_id,
        text="💬 Send your reply as your next message.\n(Send /cancel to step away.)",
    )


async def walkietalkie_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually jump back into your most recently active YAP thread."""
    user_id = update.effective_user.id
    thread_id = get_active_thread_id(user_id)

    if thread_id is None:
        await update.message.reply_text(
            "You don't have an active conversation right now. DM buttons on "
            "confessions will start one!"
        )
        return

    other_party_id = get_other_party(thread_id, user_id)
    other_identity = get_identity(other_party_id) if other_party_id else None
    other_tag = other_identity["username"] if other_identity else "them"

    await update.message.reply_text(
        f"📻 Back in your conversation with {other_tag}. Send your message!"
    )


async def _relay_yap_message(update: Update, context: ContextTypes.DEFAULT_TYPE, thread_id: int, text: str) -> None:
    sender_id = update.effective_user.id
    recipient_id = get_other_party(thread_id, sender_id)

    if recipient_id is None:
        await update.message.reply_text("This conversation isn't available anymore.")
        return

    sender_identity = get_identity(sender_id)
    sender_tag = sender_identity["username"] if sender_identity else "Someone"

    reply_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Reply", callback_data=f"yap_reply:{thread_id}")]]
    )

    try:
        await context.bot.send_message(
            chat_id=recipient_id,
            text=f"🗣️ {sender_tag} says:\n{text}",
            reply_markup=reply_keyboard,
        )
    except Exception:
        logger.warning("Failed to relay YAP message to user %s", recipient_id)
        await update.message.reply_text(
            "Couldn't deliver that — they may have blocked the bot."
        )
        return

    set_active_thread(sender_id, thread_id)
    await update.message.reply_text("Sent ✅")


async def reaction_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction_update = update.message_reaction_count
    if reaction_update is None:
        return

    message_id = reaction_update.message_id
    if not is_tracked(message_id):
        return

    total = sum(r.total_count for r in reaction_update.reactions)
    update_reaction_count(message_id, total)

    if total >= 20:
        confession = get_by_message_id(message_id)
        if confession is not None:
            await _try_award_badge(context, confession["user_id"], "viral")


async def comment_tracking_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("COMMENT_HANDLER: fired, chat_id=%s, expected=%s", update.effective_chat.id if update.effective_chat else None, CONFESSION_DISCUSSION_GROUP_ID)

    if CONFESSION_DISCUSSION_GROUP_ID is None:
        logger.info("COMMENT_HANDLER: exiting, CONFESSION_DISCUSSION_GROUP_ID is None")
        return
    if update.effective_chat.id != CONFESSION_DISCUSSION_GROUP_ID:
        logger.info("COMMENT_HANDLER: exiting, chat_id mismatch")
        return

    message = update.message
    if message is None or message.reply_to_message is None:
        logger.info("COMMENT_HANDLER: exiting, message is None or not a reply (reply_to_message=%s)", message.reply_to_message if message else "N/A")
        return

    logger.info("COMMENT_HANDLER: passed initial checks, reply_to_message_id=%s", message.reply_to_message.message_id)

    # If the comment was sent "as the group/channel" (an admin's anonymous
    # post, identified by sender_chat rather than a real from-user), there's
    # no individual Telegram user to look up an identity for — skip it.
    # This is genuinely a different case from a regular subscriber's
    # comment, which carries a real `from` user.
    if message.sender_chat is not None:
        logger.info("COMMENT_HANDLER: exiting, sent via sender_chat (sender_chat.id=%s)", message.sender_chat.id)
        return

    if update.effective_user is None:
        logger.info("COMMENT_HANDLER: exiting, effective_user is None")
        return

    replied = message.reply_to_message
    forward_origin = getattr(replied, "forward_origin", None)
    if forward_origin is None or getattr(forward_origin, "type", None) != "channel":
        logger.info("COMMENT_HANDLER: exiting, forward_origin missing or not type=channel (forward_origin=%s)", forward_origin)
        return

    channel_message_id = getattr(forward_origin, "message_id", None)
    if channel_message_id is None or not is_tracked(channel_message_id):
        logger.info("COMMENT_HANDLER: exiting, channel_message_id=%s not tracked", channel_message_id)
        return

    logger.info("COMMENT_HANDLER: proceeding to anonymize for user_id=%s", update.effective_user.id)
    commenter_id = update.effective_user.id
    identity = get_identity(commenter_id)

    if identity is None:
        # Block the comment: delete it, count nothing, DM them to set up first.
        try:
            await message.delete()
        except Exception:
            logger.warning("Couldn't delete unidentified commenter's message in discussion group")
        try:
            await context.bot.send_message(
                chat_id=commenter_id,
                text=(
                    "You'll need a username before commenting on confessions. "
                    "Send /setusername to set one up, then try commenting again!"
                ),
            )
        except Exception:
            # They may not have started the bot yet — nothing more we can do here.
            logger.warning("Couldn't DM unidentified commenter %s", commenter_id)
        return

    # Identified: repost anonymized, threaded under the same confession,
    # then delete the original real-name comment.
    safe_username = escape(identity["username"])
    safe_comment = escape(message.text or "")
    anon_text = f"{identity['avatar']} <b>{safe_username}</b> #{identity['hex_id']}:\n{safe_comment}"

    try:
        await context.bot.send_message(
            chat_id=CONFESSION_DISCUSSION_GROUP_ID,
            text=anon_text,
            reply_to_message_id=replied.message_id,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to repost anonymized comment")
        return  # don't delete the original if the repost failed — avoid losing the comment entirely

    try:
        await message.delete()
    except Exception:
        logger.warning("Failed to delete original real-name comment after reposting")

    increment_comment_count(channel_message_id)

    # Notify the original poster that someone commented, unless they're
    # commenting on their own confession (no point notifying yourself).
    confession = get_by_message_id(channel_message_id)
    if confession is not None and confession["user_id"] != commenter_id:
        try:
            await context.bot.send_message(
                chat_id=confession["user_id"],
                text=(
                    f"💬 Someone commented on your confession!\n\n"
                    f"{identity['avatar']} {identity['username']}:\n{message.text}"
                ),
            )
        except Exception:
            logger.warning("Couldn't notify poster %s about new comment", confession["user_id"])


async def confession_of_the_day_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    winner = get_todays_winner()
    if winner is None:
        return

    identity = get_identity(winner["user_id"])
    category_label = CATEGORIES.get(winner["category"], "") if winner["category"] else ""

    if identity:
        safe_username = escape(identity["username"])
        tag = f"{identity['avatar']} <b>{safe_username}</b> #{identity['hex_id']}"
    else:
        tag = "Anonymous"

    safe_text = escape(winner["confession_text"])

    text = (
        "🌟 Confession of the Day 🌟\n\n"
        f"{category_label}\n\n"
        f"{tag}:\n{safe_text}\n\n"
        f"({winner['reaction_count']} reactions, {winner['comment_count']} comments)"
    )

    try:
        await context.bot.send_message(
            chat_id=CONFESSION_CHANNEL_ID, text=text, parse_mode="HTML"
        )
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


async def finalize_1v1_matches_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic job: finds any 1v1 trivia matches whose 24h window has
    passed, settles them (winner/tie), awards a modest bonus to the
    winner, and DMs both players the result. Without this job, an
    expired match would otherwise only get finalized lazily the next
    time either player happens to check /trivia1v1 — which could be
    never, leaving the match stuck in limbo and neither player notified.
    """
    finalized = finalize_expired_matches()

    WINNER_BONUS = 25  # small extra incentive on top of the per-question points already earned

    for match in finalized:
        player_a_identity = get_identity(match["player_a_id"])
        player_b_identity = get_identity(match["player_b_id"]) if match["player_b_id"] else None

        name_a = player_a_identity["username"] if player_a_identity else "Player A"
        name_b = player_b_identity["username"] if player_b_identity else "Player B"

        if match["winner_id"] is None:
            result_text_a = f"⚔️ Your 1v1 trivia match vs {name_b} ended in a TIE!\nFinal score: {match['score_a']} - {match['score_b']}"
            result_text_b = f"⚔️ Your 1v1 trivia match vs {name_a} ended in a TIE!\nFinal score: {match['score_b']} - {match['score_a']}"
        elif match["winner_id"] == match["player_a_id"]:
            award_points(match["player_a_id"], TRIVIA_1V1_GAME_NAME, WINNER_BONUS)
            result_text_a = f"🎉 You WON your 1v1 trivia match vs {name_b}!\nFinal score: {match['score_a']} - {match['score_b']}\n+{WINNER_BONUS} bonus points!"
            result_text_b = f"⚔️ Your 1v1 trivia match vs {name_a} ended.\nFinal score: {match['score_b']} - {match['score_a']}\nBetter luck next time!"
        else:
            award_points(match["player_b_id"], TRIVIA_1V1_GAME_NAME, WINNER_BONUS)
            result_text_a = f"⚔️ Your 1v1 trivia match vs {name_b} ended.\nFinal score: {match['score_a']} - {match['score_b']}\nBetter luck next time!"
            result_text_b = f"🎉 You WON your 1v1 trivia match vs {name_a}!\nFinal score: {match['score_b']} - {match['score_a']}\n+{WINNER_BONUS} bonus points!"

        try:
            await context.bot.send_message(chat_id=match["player_a_id"], text=result_text_a)
        except Exception:
            logger.warning("Couldn't notify player_a (%s) of 1v1 match result", match["player_a_id"])

        if match["player_b_id"]:
            try:
                await context.bot.send_message(chat_id=match["player_b_id"], text=result_text_b)
            except Exception:
                logger.warning("Couldn't notify player_b (%s) of 1v1 match result", match["player_b_id"])


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
    init_badges_db()
    init_yap_db()
    init_trivia_cache_db()
    init_daily_trivia_db()
    init_trivia_1v1_db()

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
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("badges", badges_command))
    app.add_handler(CommandHandler("myconfessions", myconfessions_command))
    app.add_handler(CommandHandler("random", random_command))
    app.add_handler(CommandHandler("WalkieTalkie", walkietalkie_command))
    app.add_handler(CommandHandler("cancel", general_cancel))

    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_GAMES}$"), games_button))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_MYPOINTS}$"), mypoints_button))
    app.add_handler(CommandHandler("rank", rank_command))
    app.add_handler(CommandHandler("trivia", trivia_command))
    app.add_handler(CallbackQueryHandler(trivia_answer_callback, pattern=r"^trivia_answer:"))
    app.add_handler(CommandHandler("trivia1v1", trivia1v1_command))
    app.add_handler(CallbackQueryHandler(trivia1v1_button_callback, pattern=r"^trivia1v1_(new|join:)"))
    app.add_handler(CommandHandler("trivia1v1answer", trivia1v1_answer_command))
    app.add_handler(CallbackQueryHandler(trivia1v1_answer_callback, pattern=r"^trivia1v1_answer:"))
    app.add_handler(CommandHandler("fish", fish_command))
    app.add_handler(CommandHandler("reel", reel_command))
    app.add_handler(CommandHandler("tank", tank_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("reports", reports_command))
    app.add_handler(CommandHandler("resolve", resolve_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("whois", whois_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("addpoints", addpoints_command))
    app.add_handler(CommandHandler("pointlog", pointlog_command))

    app.add_handler(CallbackQueryHandler(games_menu_choice, pattern=r"^game:"))
    app.add_handler(CallbackQueryHandler(gossip_from_post_callback, pattern=r"^gossip_from_post$"))
    app.add_handler(CallbackQueryHandler(yap_start_callback, pattern=r"^yap_start:"))
    app.add_handler(CallbackQueryHandler(yap_reply_callback, pattern=r"^yap_reply:"))

    # Catch-all for plain text: Wordle guesses or a hint, depending on state.
    if CONFESSION_DISCUSSION_GROUP_ID is not None:
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=CONFESSION_DISCUSSION_GROUP_ID) & filters.REPLY,
                comment_tracking_handler,
            )
        )

    # Catch-all for plain text DMs (Wordle guesses, YAP relay, or a hint).
    # This MUST be registered after the discussion-group comment handler
    # above — both match plain text messages, and PTB stops checking
    # further handlers in the same group once one matches. Registering
    # this first was the actual root cause of comments never reaching
    # comment_tracking_handler at all: every comment is plain text, so it
    # matched filters.TEXT & ~filters.COMMAND here and got swallowed
    # before the comment handler (registered later) ever got a turn.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text_handler))

    app.add_handler(
        MessageReactionHandler(
            reaction_update_handler,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_COUNT_UPDATED,
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

    # Streak warning — 10:30pm Singapore time, shortly before the SGT
    # midnight cutoff used by Wordle/streaks.
    streak_job = app.job_queue.run_daily(
        streak_warning_job,
        time=dt_time(hour=22, minute=30, tzinfo=SGT),
    )
    logger.info("Scheduled streak_warning_job: %s", streak_job)

    # Reaction-points settlement — checks every 10 minutes for confessions
    # that have crossed the 1-hour mark and haven't been paid out yet.
    app.job_queue.run_repeating(
        settle_reaction_points_job,
        interval=timedelta(minutes=10),
        first=timedelta(seconds=30),
    )

    # 1v1 trivia match finalization — checks every 10 minutes for matches
    # whose 24h window has passed, settles winner/tie, and notifies both
    # players. Without this, a match would only get finalized whenever
    # either player happened to next check /trivia1v1, possibly never.
    app.job_queue.run_repeating(
        finalize_1v1_matches_job,
        interval=timedelta(minutes=10),
        first=timedelta(minutes=1),
    )

    # Daily leaderboard — posts this week's standings to the channel once
    # a day at midnight SGT, in addition to the dedicated Sunday-evening
    # recap. (Previously every 2 hours — changed to daily since the more
    # frequent posts were too noisy for the channel.)
    app.job_queue.run_daily(
        periodic_leaderboard_job,
        time=dt_time(hour=0, minute=0, tzinfo=SGT),
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
