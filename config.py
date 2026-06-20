"""Centralized config, loaded from environment variables (.env supported via python-dotenv)."""

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", f"/webhook/{BOT_TOKEN}")

PORT = int(os.environ.get("PORT", 8443))

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

CONFESSION_CHANNEL_ID = int(os.environ["CONFESSION_CHANNEL_ID"])

_discussion_group_raw = os.environ.get("CONFESSION_DISCUSSION_GROUP_ID", "")
CONFESSION_DISCUSSION_GROUP_ID = int(_discussion_group_raw) if _discussion_group_raw else None

ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "8608439807"))
