"""ESPN odds: opening + current moneyline per game, free, no API key.

Ported from the MLB repo's espn.py (same hidden ESPN endpoints, swapped to
basketball/nba). ESPN carries a real open->current moneyline for nearly
every game, which is why this replaces needing a keyed odds provider.

NOT YET TESTED against live data -- espn.com is blocked by this
environment's egress policy the same as everything else; verify field
paths once network access is available (set ESPN_DEBUG=1 for one run to
dump raw JSON into output/espn_debug/, same pattern as the MLB repo).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.db.connection import get_connection

from .nba_teams import canon_abbr

log = logging.getLogger("espn_source")
SOURCE_NAME = "espn"

DEBUG = os.environ.get("ESPN_DEBUG") == "1"
DEBUG_DIR = Path("data/espn_debug")
HEADERS = {"User-Agent": "nba-edge-finder (personal research)"}
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"
EVENT_ODDS = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
              "events/{eid}/competitions/{eid}/odds")


def _get(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # network/HTTP/JSON -- degrade gracefully
        log.warning("espn fetch failed (%s): %s", url, exc)
        return None
    if DEBUG:
        _dump(url, data)
    return data


def _dump(url: str, data: dict) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"\W+", "_", url.split("://", 1)[-1])[:120] + ".json"
    (DEBUG_DIR / name).write_text(json.dumps(data, indent=2)[:2_000_000])


def _events(date: str) -> list[dict]:
    """[{eid, away_abbr, home_abbr}] for the date (date = YYYY-MM-DD)."""
    data = _get(SCOREBOARD.format(date.replace("-", "")))
    if not data:
        return []
    out: list[dict] = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        abbr = {}
        for c in comp.get("competitors", []):
            abbr[c.get("homeAway")] = (c.get("team") or {}).get("abbreviation")
        if abbr.get("home") and abbr.get("away"):
            out.append({"eid": ev.get("id"), "away_abbr": abbr["away"], "home_abbr": abbr["home"]})
    return out


def _american(ml: dict | None) -> int | None:
    if not isinstance(ml, dict):
        return None
    a = ml.get("american")
    if a in (None, "", "OFF"):
        return None
    if str(a).upper() == "EVEN":
        return 100
    try:
        return int(str(a).replace("+", ""))
    except ValueError:
        return None


def _side(team_odds: dict | None) -> tuple[int | None, int | None]:
    t = team_odds or {}
    op = _american(t.get("open", {}).get("moneyLine"))
    cur = _american(t.get("current", {}).get("moneyLine"))
    if cur is None and isinstance(t.get("moneyLine"), (int, float)):
        cur = int(t["moneyLine"])
    return op, cur


def lines(date: str) -> list[dict]:
    """Open + current moneyline for every game on the date. {away_abbr,
    home_abbr, away_open, home_open, away_current, home_current}."""
    out: list[dict] = []
    for e in _events(date):
        data = _get(EVENT_ODDS.format(eid=e["eid"]))
        items = (data or {}).get("items") or []
        if not items:
            continue
        it = items[0]  # primary provider
        ao, ac = _side(it.get("awayTeamOdds"))
        ho, hc = _side(it.get("homeTeamOdds"))
        if ac is None and hc is None:
            continue
        out.append({"away_abbr": e["away_abbr"], "home_abbr": e["home_abbr"],
                    "away_open": ao, "home_open": ho,
                    "away_current": ac, "home_current": hc})
        time.sleep(0.3)
    log.info("espn: parsed %d game line(s)", len(out))
    return out


def historical_lines(date: str) -> list[dict]:
    """Open + CLOSE moneylines for the date's games -- works for
    COMPLETED games, which is the whole point: verified against a real
    Final game (diagnose_espn_historical_odds.py, 2025-10-22 MIA@ORL)
    that the per-event odds endpoint keeps serving after the game ends,
    with explicit open/close/current blocks per team (home opened -380,
    closed -340; away opened +290, closed +270). The 'ESPN BET' provider
    item (priority 0) carries the close block; the 'Live Odds' item does
    not, so prefer whichever item actually has a close."""
    out: list[dict] = []
    for e in _events(date):
        data = _get(EVENT_ODDS.format(eid=e["eid"]))
        items = (data or {}).get("items") or []
        if not items:
            continue
        it = next((i for i in items
                   if isinstance((i.get("homeTeamOdds") or {}).get("close"), dict)),
                  items[0])
        home, away = it.get("homeTeamOdds") or {}, it.get("awayTeamOdds") or {}
        row = {
            "away_abbr": e["away_abbr"], "home_abbr": e["home_abbr"],
            "away_open": _american((away.get("open") or {}).get("moneyLine")),
            "home_open": _american((home.get("open") or {}).get("moneyLine")),
            "away_close": _american((away.get("close") or {}).get("moneyLine")),
            "home_close": _american((home.get("close") or {}).get("moneyLine")),
        }
        if row["home_close"] is not None or row["home_open"] is not None:
            out.append(row)
        time.sleep(0.3)
    log.info("espn historical: parsed %d game line(s) for %s", len(out), date)
    return out


def backfill_closing_odds(season: str, db_path=None) -> dict:
    """Write opening + closing moneylines for every completed game in the
    season that doesn't have a closing odds_snapshots row yet -- the
    free replacement for SBR's bot-walled historical archive, since
    ESPN's odds endpoint keeps serving open/close for past games (see
    historical_lines). run_backtest._closing_moneyline reads any
    line_type='closing' row, so backfilled games get real ROI."""
    conn = get_connection(db_path) if db_path else get_connection()
    dates = [r["game_date"] for r in conn.execute(
        """SELECT DISTINCT game_date FROM games
           WHERE season = ? AND status = 'final'
             AND game_id NOT IN (
                 SELECT game_id FROM odds_snapshots WHERE line_type = 'closing'
             )
           ORDER BY game_date""",
        (season,),
    ).fetchall()]

    now = datetime.now(timezone.utc).isoformat()
    opening_written, closing_written = 0, 0
    for date in dates:
        for row in historical_lines(date):
            home, away = canon_abbr(row["home_abbr"]), canon_abbr(row["away_abbr"])
            game = conn.execute(
                "SELECT game_id FROM games WHERE home_team = ? AND away_team = ? AND game_date = ?",
                (home, away, date),
            ).fetchone()
            if not game:
                continue
            game_id = game["game_id"]
            if row["home_open"] is not None and not conn.execute(
                "SELECT 1 FROM odds_snapshots WHERE game_id = ? AND line_type = 'opening' LIMIT 1",
                (game_id,),
            ).fetchone():
                conn.execute(
                    """INSERT INTO odds_snapshots
                       (game_id, source, book, snapshot_time, line_type,
                        home_moneyline, away_moneyline)
                       VALUES (?, ?, 'espn', ?, 'opening', ?, ?)""",
                    (game_id, SOURCE_NAME, now, row["home_open"], row["away_open"]),
                )
                opening_written += 1
            if row["home_close"] is not None and not conn.execute(
                "SELECT 1 FROM odds_snapshots WHERE game_id = ? AND line_type = 'closing' LIMIT 1",
                (game_id,),
            ).fetchone():
                conn.execute(
                    """INSERT INTO odds_snapshots
                       (game_id, source, book, snapshot_time, line_type,
                        home_moneyline, away_moneyline)
                       VALUES (?, ?, 'espn', ?, 'closing', ?, ?)""",
                    (game_id, SOURCE_NAME, now, row["home_close"], row["away_close"]),
                )
                closing_written += 1
        conn.commit()  # per-date, so partial progress survives a later failure

    conn.close()
    log.info("espn historical backfill: %d opening + %d closing rows across %d date(s)",
             opening_written, closing_written, len(dates))
    return {"opening_written": opening_written, "closing_written": closing_written,
            "dates_checked": len(dates)}


def poll_and_snapshot(date: str, db_path=None) -> dict:
    """Fetch ESPN's open+current moneyline for the date's games and write
    odds_snapshots rows: the 'open' value once (as line_type='opening', on
    the first poll that sees it), the 'current' value every poll
    (line_type='live', or 'closing' if this is likely the last poll before
    tip-off is left to the caller/line_movement analysis to interpret --
    ESPN's 'current' is simply whatever price is live right now)."""
    conn = get_connection(db_path) if db_path else get_connection()
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for row in lines(date):
        home, away = canon_abbr(row["home_abbr"]), canon_abbr(row["away_abbr"])
        game = conn.execute(
            "SELECT game_id FROM games WHERE home_team = ? AND away_team = ? AND game_date = ?",
            (home, away, date),
        ).fetchone()
        if not game:
            continue
        game_id = game["game_id"]

        already_seen = conn.execute(
            "SELECT 1 FROM odds_snapshots WHERE game_id = ? AND source = ? LIMIT 1",
            (game_id, SOURCE_NAME),
        ).fetchone()
        if not already_seen and row["home_open"] is not None:
            conn.execute(
                """INSERT INTO odds_snapshots
                   (game_id, source, book, snapshot_time, line_type,
                    home_moneyline, away_moneyline)
                   VALUES (?, ?, 'espn', ?, 'opening', ?, ?)""",
                (game_id, SOURCE_NAME, now, row["home_open"], row["away_open"]),
            )
            written += 1
        if row["home_current"] is not None:
            conn.execute(
                """INSERT INTO odds_snapshots
                   (game_id, source, book, snapshot_time, line_type,
                    home_moneyline, away_moneyline)
                   VALUES (?, ?, 'espn', ?, 'live', ?, ?)""",
                (game_id, SOURCE_NAME, now, row["home_current"], row["away_current"]),
            )
            written += 1

    conn.commit()
    conn.close()
    return {"snapshots_written": written}
