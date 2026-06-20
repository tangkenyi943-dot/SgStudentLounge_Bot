"""Centralized config, loaded from environment variables (.env supported via python-dotenv)."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]  # raises clearly if missing — fail fast
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # e.g. https://yourapp.com  (no trailing slash needed)

# Use the bot token in the path so the endpoint isn't guessable by outsiders.
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", f"/webhook/{BOT_TOKEN}")

PORT = int(os.environ.get("PORT", 8443))

# Optional: Telegram will send this back in a header on every webhook request;
# verify it server-side if you add your own reverse proxy / extra validation.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# The channel where confessions get posted. Must be a negative chat ID
# (e.g. -1003717925471) for channels/groups. The bot must be an admin
# there with permission to post messages.
CONFESSION_CHANNEL_ID = int(os.environ["CONFESSION_CHANNEL_ID"])

# The linked discussion group under the channel, used for tracking comment
# counts for Confession of the Day. Optional — if not set, comment
# tracking is simply skipped and only reactions count toward the score.
_discussion_group_raw = os.environ.get("CONFESSION_DISCUSSION_GROUP_ID", "")
CONFESSION_DISCUSSION_GROUP_ID = int(_discussion_group_raw) if _discussion_group_raw else None

# Telegram user ID of the bot owner/admin, used to gate admin-only commands
# like /reports. Get your own ID by messaging @RawDataBot or similar.
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "8608439807"))

# Public @username of the confession channel (no @ prefix), used to build
# clickable links back to specific posts, e.g. for /random.
CONFESSION_CHANNEL_USERNAME = os.environ.get("CONFESSION_CHANNEL_USERNAME", "SgStudentLounge")

# Directory where the SQLite database file lives. On Render, set this to
# your persistent disk's mount path (e.g. "/data") so confessions.db
# survives deploys and restarts — without this, the database sits on the
# service's ephemeral filesystem and gets wiped on every restart. Defaults
# to the project folder for local development, where a persistent disk
# isn't relevant.
DB_DIR = os.environ.get("DB_DIR", str(Path(__file__).parent))
