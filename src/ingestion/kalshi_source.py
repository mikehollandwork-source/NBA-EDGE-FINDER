"""Kalshi (CFTC-regulated prediction market) NBA game markets -- read-only,
ported from the MLB repo's kalshi.py. A second independent real-money
venue alongside Polymarket, so sharp/public divergence isn't reliant on
just one market.

Kalshi books quote YES bids and NO bids in cents: the YES ask is 100
minus the best NO bid.

UNVERIFIED: the MLB repo hardcodes SERIES = "KXMLBGAME" (found by
inspecting a live request); "KXNBAGAME" below follows the same naming
convention but isn't confirmed. `_resolve_series()` falls back to
searching Kalshi's public series listing for anything NBA-tagged if the
hardcoded guess returns nothing, so a wrong guess degrades to a slower
lookup rather than silently returning zero games forever. Set
KALSHI_DEBUG=1 to dump raw JSON into data/kalshi_debug/ on the first live
run to confirm/fix the ticker.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.db.connection import get_connection

from .nba_teams import name_to_abbr

log = logging.getLogger("kalshi_source")
SOURCE_NAME = "kalshi"

BASE = "https://api.elections.kalshi.com/trade-api/v2"
GUESSED_SERIES = "KXNBAGAME"
TIMEOUT = 15
DEBUG = os.environ.get("KALSHI_DEBUG") == "1"
DEBUG_DIR = Path("data/kalshi_debug")

_SERIES_CACHE: str | None = None


def _get(path: str, **params):
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "nba-edge-finder (personal research)",
                                  "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        if DEBUG:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            name = (path.strip("/").replace("/", "_") or "root") + ".json"
            (DEBUG_DIR / name).write_text(json.dumps(data)[:800_000])
        return data
    except Exception as exc:
        log.warning("kalshi fetch failed (%s %s): %s", path, params, exc)
        return None


def _resolve_series() -> str:
    """The guessed ticker if it actually returns open markets; otherwise
    search the series listing for something NBA-tagged. Caches whichever
    works so we don't re-probe every call."""
    global _SERIES_CACHE
    if _SERIES_CACHE is not None:
        return _SERIES_CACHE

    probe = _get("/markets", series_ticker=GUESSED_SERIES, status="open", limit=1)
    if (probe or {}).get("markets"):
        _SERIES_CACHE = GUESSED_SERIES
        return _SERIES_CACHE

    data = _get("/series", category="Sports")
    for s in (data or {}).get("series", []) or []:
        title = str(s.get("title", "")).upper()
        if "NBA" in title and "GAME" in title:
            _SERIES_CACHE = s.get("ticker", GUESSED_SERIES)
            log.info("kalshi: resolved NBA series ticker to %s (guess was %s)",
                     _SERIES_CACHE, GUESSED_SERIES)
            return _SERIES_CACHE

    log.warning("kalshi: could not confirm or discover an NBA series ticker")
    _SERIES_CACHE = GUESSED_SERIES
    return _SERIES_CACHE


def game_markets() -> dict:
    """{(abbr1, abbr2): {abbr: ticker}} for OPEN NBA game markets, keyed
    both orders."""
    series = _resolve_series()
    events: dict = {}
    cursor = None
    for _ in range(20):
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", **params)
        mkts = (data or {}).get("markets") or []
        for m in mkts:
            try:
                team = name_to_abbr(str(m.get("yes_sub_title") or m.get("subtitle") or ""))
                ev = m.get("event_ticker")
                if team and ev and m.get("ticker"):
                    events.setdefault(ev, {})[team] = m["ticker"]
            except Exception:
                continue
        cursor = (data or {}).get("cursor")
        if not cursor or not mkts:
            break
        time.sleep(0.2)
    index: dict = {}
    for tick_by_team in events.values():
        abbrs = list(tick_by_team)
        if len(abbrs) == 2:
            index[(abbrs[0], abbrs[1])] = tick_by_team
            index[(abbrs[1], abbrs[0])] = tick_by_team
    log.info("kalshi: %d open game market pair(s)", len(index) // 2)
    return index


def top_of_book(ticker: str) -> dict | None:
    """{bid, ask, bid_sz, ask_sz} for a market's YES side, prices in 0-1.
    Empty book -> {'empty': True}; failure -> None."""
    data = _get(f"/markets/{ticker}/orderbook")
    ob = (data or {}).get("orderbook")
    if ob is None:
        return None
    try:
        yes = [(int(p) / 100.0, int(q)) for p, q in ob.get("yes") or []]
        no = [(int(p) / 100.0, int(q)) for p, q in ob.get("no") or []]
    except Exception:
        return None
    if not yes and not no:
        return {"empty": True}
    out: dict = {}
    if yes:
        p, q = max(yes, key=lambda x: x[0])
        out["bid"], out["bid_sz"] = p, q
    if no:
        p, q = max(no, key=lambda x: x[0])
        out["ask"], out["ask_sz"] = round(1 - p, 2), q
    return out


def snapshot_game(game_id: str, home_abbr: str, away_abbr: str, db_path=None) -> dict:
    """Snapshot Kalshi's top-of-book implied probability for one game, if
    an open market exists for it. Uses the best bid on each side's YES
    token as that side's probability estimate (same top-of-book approach
    as polymarket_source, so the two independent markets read the same
    way)."""
    index = game_markets()
    tokens = index.get((away_abbr, home_abbr))
    if not tokens:
        return {"found": False}

    home_book = top_of_book(tokens.get(home_abbr, ""))
    away_book = top_of_book(tokens.get(away_abbr, ""))
    home_prob = (home_book or {}).get("bid")
    away_prob = (away_book or {}).get("bid")
    if home_prob is None or away_prob is None:
        return {"found": False, "reason": "empty book"}

    conn = get_connection(db_path) if db_path else get_connection()
    already_seen = conn.execute(
        "SELECT 1 FROM odds_snapshots WHERE game_id = ? AND source = ? LIMIT 1",
        (game_id, SOURCE_NAME),
    ).fetchone()
    line_type = "opening" if not already_seen else "live"
    conn.execute(
        """INSERT INTO odds_snapshots
           (game_id, source, book, snapshot_time, line_type,
            home_implied_prob, away_implied_prob)
           VALUES (?, ?, 'kalshi', ?, ?, ?, ?)""",
        (game_id, SOURCE_NAME, datetime.now(timezone.utc).isoformat(),
         line_type, home_prob, away_prob),
    )
    conn.commit()
    conn.close()
    return {"found": True, "home_prob": home_prob, "away_prob": away_prob}
