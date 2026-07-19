"""Sends the hourly summary to Telegram -- free, no rate-limit concerns
for personal use (Telegram's Bot API has generous limits for a single
chat).

Setup (one-time, on your end -- can't be done on your behalf):
  1. Message @BotFather on Telegram, /newbot, follow the prompts -> you
     get a bot token.
  2. Message your new bot anything, then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id
     in the response.
  3. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as GitHub Actions repo
     secrets (Settings -> Secrets and variables -> Actions). Don't paste
     the actual token/chat_id anywhere in chat or commit it to the repo.

NOT YET TESTED against live data -- api.telegram.org is not in this
environment's egress allowlist either; verify once network access is
sorted (this one doesn't require an environment policy change if it
only ever runs from GitHub Actions, which has normal internet access).
"""

import os

import requests

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_message(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")

    resp = requests.post(
        f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    resp.raise_for_status()
