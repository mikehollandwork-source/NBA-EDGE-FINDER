"""Pinnacle (sharp-origin book) moneylines via the guest web API -- best
effort, ported from the MLB repo's pinnacle.py.

Pinnacle is a market-making book: its number is the closest free proxy to
the low-limit "test line" sharps shoot at before retail books copy it. The
X-API-Key below is the public client key shipped in pinnacle.com's own web
bundle (not an account secret) -- same one the MLB repo uses, since it's
not sport-specific.

Unlike the MLB repo (which hardcodes MLB's league id = 246, presumably
found by inspecting a live request), NBA's league id isn't something we
can verify from here -- this environment can't reach pinnacle.com.
Instead of guessing a number that's probably wrong, `_nba_league_id()`
looks it up from Pinnacle's own /sports listing and caches it, failing
soft (None) if the listing shape doesn't match what's expected here. Set
PINNACLE_DEBUG=1 to dump that listing for inspection on the first live run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.db.connection import get_connection

from .nba_teams import FULL_TO_ABBR, name_to_abbr

log = logging.getLogger("pinnacle_source")
SOURCE_NAME = "pinnacle"

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
HEADERS = {
    "User-Agent": "nba-edge-finder (personal research)",
    "X-API-Key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",
    "Accept": "application/json",
}
DEBUG = os.environ.get("PINNACLE_DEBUG") == "1"
DEBUG_DIR = Path("data/pinnacle_debug")

_LEAGUE_ID_CACHE: int | None = None


def _get(path: str):
    try:
        r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("pinnacle fetch failed (%s): %s", path, exc)
        return None
    if DEBUG:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        name = path.strip("/").replace("/", "_") + ".json"
        (DEBUG_DIR / name).write_text(json.dumps(data, indent=2)[:800_000])
    return data


def _nba_league_id() -> int | None:
    global _LEAGUE_ID_CACHE
    if _LEAGUE_ID_CACHE is not None:
        return _LEAGUE_ID_CACHE
    data = _get("/sports")
    for sport in data if isinstance(data, list) else []:
        for league in sport.get("leagues", []) or []:
            if str(league.get("name", "")).strip().upper() in ("NBA", "NBA BASKETBALL"):
                _LEAGUE_ID_CACHE = league.get("id")
                return _LEAGUE_ID_CACHE
    log.warning("pinnacle: could not resolve NBA league id from /sports listing")
    return None


def lines() -> list[dict]:
    """[{away_name, home_name, away_ml, home_ml, start}] for NBA games
    currently on Pinnacle's board. Empty on any failure -- optional source."""
    league_id = _nba_league_id()
    if league_id is None:
        return []
    matchups = _get(f"/leagues/{league_id}/matchups")
    markets = _get(f"/leagues/{league_id}/markets/straight")
    teams: dict = {}
    for m in matchups if isinstance(matchups, list) else []:
        try:
            if m.get("parent") or m.get("type") not in (None, "matchup"):
                continue
            parts = {p.get("alignment"): p.get("name") for p in m.get("participants", [])}
            if parts.get("home") and parts.get("away"):
                teams[m["id"]] = {"away_name": parts["away"], "home_name": parts["home"],
                                  "start": m.get("startTime")}
        except Exception:
            continue
    out: list[dict] = []
    for mk in markets if isinstance(markets, list) else []:
        try:
            if mk.get("type") != "moneyline" or mk.get("period") != 0:
                continue
            t = teams.get(mk.get("matchupId"))
            if not t:
                continue
            prices = {p.get("designation"): p.get("price") for p in mk.get("prices", [])}
            if prices.get("away") is None or prices.get("home") is None:
                continue
            out.append({**t, "away_ml": int(prices["away"]), "home_ml": int(prices["home"])})
        except Exception:
            continue
    log.info("pinnacle: parsed %d game line(s)", len(out))
    return out


def _to_abbr(name: str) -> str | None:
    return FULL_TO_ABBR.get(name) or name_to_abbr(name)


def poll_and_snapshot(date: str, db_path=None) -> dict:
    """Match Pinnacle's current board to games by team name and write a
    'live' odds_snapshots row per match -- Pinnacle's own API doesn't
    expose an explicit open/close split the way ESPN's does, so every
    poll is recorded as a live reading of the sharp-book price."""
    conn = get_connection(db_path) if db_path else get_connection()
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for row in lines():
        home, away = _to_abbr(row["home_name"]), _to_abbr(row["away_name"])
        if not home or not away:
            continue
        game = conn.execute(
            "SELECT game_id FROM games WHERE home_team = ? AND away_team = ? AND game_date = ?",
            (home, away, date),
        ).fetchone()
        if not game:
            continue
        conn.execute(
            """INSERT INTO odds_snapshots
               (game_id, source, book, snapshot_time, line_type,
                home_moneyline, away_moneyline)
               VALUES (?, ?, 'pinnacle', ?, 'live', ?, ?)""",
            (game["game_id"], SOURCE_NAME, now, row["home_ml"], row["away_ml"]),
        )
        written += 1
    conn.commit()
    conn.close()
    return {"snapshots_written": written}
