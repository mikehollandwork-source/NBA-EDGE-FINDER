"""Reddit as a second public-opinion forum, to compare against covers
consensus -- ported from the MLB repo's reddit.py, same technique (public
JSON API, mention-tally reusing the covers forum's moneyline-vs-spread
parsing logic) pointed at NBA-relevant subreddits.

Reddit blocks anonymous *.json reads from datacenter IPs (GitHub Actions
gets a 403), so this uses the sanctioned application-only OAuth API when
REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set (free to create at
reddit.com/prefs/apps -- a "script" app), and fails soft to zero counts
otherwise. Not required for the rest of the pipeline to run.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
from pathlib import Path

import requests

from .covers_source import _post_moneyline_teams, _team_patterns

log = logging.getLogger("reddit_source")

SUBREDDITS = ["nba", "sportsbook", "sportsbetting"]
WWW_BASE = "https://www.reddit.com"
OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
LISTING = "{base}/r/{sub}/new.json?limit=50&raw_json=1"
THREAD = "{base}{permalink}.json?limit=200&depth=4&raw_json=1"

MAX_THREADS = 12
THREAD_AGE_HOURS = 36
TIMEOUT = 20
POLITE_DELAY = 1.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "nba-edge-finder/1.0 (personal betting research)"})

DEBUG = os.environ.get("REDDIT_DEBUG") == "1"
DEBUG_DIR = Path("data/reddit_debug")


def _oauth_token() -> str | None:
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        return None
    try:
        resp = requests.post(TOKEN_URL, auth=(cid, sec),
                             data={"grant_type": "client_credentials"},
                             headers={"User-Agent": SESSION.headers["User-Agent"]},
                             timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:
        log.warning("reddit OAuth token failed: %s", exc)
        return None


def _get_json(url: str) -> dict | list | None:
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        time.sleep(POLITE_DELAY)
        data = resp.json()
        if DEBUG:
            _dump(url, resp.text)
        return data
    except Exception as exc:
        log.warning("reddit fetch failed for %s: %s", url, exc)
        return None


def _dump(url: str, text: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"\W+", "_", url.split("//", 1)[-1])[:80] + ".json"
        (DEBUG_DIR / name).write_text(text, encoding="utf-8")
    except Exception as exc:
        log.warning("reddit debug dump failed: %s", exc)


def _is_nba(title: str) -> bool:
    t = title.lower()
    if any(w in t for w in ("mlb", "nfl", "nhl", "soccer", "ufc", "tennis")):
        return False
    return any(w in t for w in ("nba", "basketball", "pick", "play", "parlay",
                                "daily", "moneyline", "what are"))


def _recent_threads(sub: str, now: dt.datetime, base: str) -> list[str]:
    data = _get_json(LISTING.format(base=base, sub=sub))
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        created = dt.datetime.fromtimestamp(d.get("created_utc", 0), tz=dt.timezone.utc)
        if (now - created).total_seconds() > THREAD_AGE_HOURS * 3600:
            continue
        if not _is_nba(d.get("title", "")):
            continue
        if d.get("permalink"):
            out.append(d["permalink"])
        if len(out) >= MAX_THREADS:
            break
    return out


def _walk_comments(node, bodies: list[str]) -> None:
    if isinstance(node, dict):
        for c in node.get("data", {}).get("children", []):
            d = c.get("data", {})
            body = d.get("body")
            if body:
                bodies.append(body)
            replies = d.get("replies")
            if isinstance(replies, dict):
                _walk_comments(replies, bodies)


def _thread_bodies(permalink: str, base: str) -> list[str]:
    data = _get_json(THREAD.format(base=base, permalink=permalink))
    bodies: list[str] = []
    if isinstance(data, list) and len(data) >= 2:
        if data[0].get("data", {}).get("children"):
            sel = data[0]["data"]["children"][0].get("data", {}).get("selftext")
            if sel:
                bodies.append(sel)
        _walk_comments(data[1], bodies)
    return bodies


def reddit_majority(teams: list[tuple[str, str]], date: str) -> dict[str, int]:
    """Tally team moneyline mentions across recent NBA-relevant subreddit
    threads, using the same mention logic as the covers forum tally."""
    counts = {name: 0 for name, _ in teams}
    matchers = {name: _team_patterns(name, abbr) for name, abbr in teams}
    now = dt.datetime.now(dt.timezone.utc)

    token = _oauth_token()
    base = OAUTH_BASE if token else WWW_BASE
    if token:
        SESSION.headers["Authorization"] = f"bearer {token}"
    else:
        log.info("reddit: no OAuth credentials -- anonymous reads are blocked from "
                 "datacenter IPs, tally will be empty (set REDDIT_CLIENT_ID/SECRET, "
                 "free to create)")

    threads = 0
    for sub in SUBREDDITS:
        for permalink in _recent_threads(sub, now, base):
            threads += 1
            for body in _thread_bodies(permalink, base):
                for name in _post_moneyline_teams(body.lower(), matchers):
                    counts[name] += 1
    log.info("reddit: tallied %d thread(s) across %d sub(s) [auth=%s]",
             threads, len(SUBREDDITS), bool(token))
    return counts
