"""Centralized config, loaded from environment variables (.env supported via python-dotenv)."""

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", f"/webhook/{BOT_TOKEN}")

PORT = int(os.environ.get("PORT", 8443))

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
