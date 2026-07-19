"""Sends the hourly summary to Telegram -- free, no rate-limit concerns
for personal use. Upgraded to match the MLB repo's notify.py pattern,
which is meaningfully more robust than the original version of this file:
supports multiple recipients, splits long messages under Telegram's
4096-char cap, and no-ops quietly (logs, doesn't raise) when secrets
aren't set, so the pipeline runs fine before/without Telegram configured
instead of crashing the whole hourly run.

Setup (one-time, on your end -- can't be done on your behalf):
  1. Message @BotFather on Telegram, /newbot, follow the prompts -> you
     get a bot token.
  2. Message your new bot anything, then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
  3. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as GitHub Actions repo
     secrets. TELEGRAM_CHAT_ID can be a comma/space-separated list to
     fan out to more than one chat/channel.

NOT YET TESTED against live data -- api.telegram.org is not in this
environment's egress allowlist; this doesn't require an environment
policy change if it only ever runs from GitHub Actions, which has
normal internet access.
"""

import logging
import os

import requests

log = logging.getLogger("telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"
LIMIT = 3900  # Telegram caps a message at 4096 chars; stay safely under


def _chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split into <=limit pieces on line boundaries."""
    out, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def _chat_ids(raw: str) -> list[str]:
    return [c.strip() for c in raw.replace(",", " ").split() if c.strip()]


def send_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chats = _chat_ids(os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not token or not chats:
        log.info("telegram not configured (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); skipping")
        return False

    parts = _chunks(text)
    ok = True
    for chat in chats:
        for i, part in enumerate(parts, 1):
            try:
                resp = requests.post(
                    f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": part, "parse_mode": "Markdown"},
                    timeout=20,
                )
                resp.raise_for_status()
            except Exception as exc:
                log.warning("telegram send failed (chat %s, part %d/%d): %s",
                           chat, i, len(parts), exc)
                ok = False
    if ok:
        log.info("telegram message sent to %d chat(s), %d part(s) each", len(chats), len(parts))
    return ok
