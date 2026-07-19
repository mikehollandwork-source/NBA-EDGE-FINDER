"""Live odds polling via The Odds API (free tier, ~500 requests/month).

Used going forward (not for backtesting): polled on a schedule to build
our own line-movement history, since the free tier does not retain
historical odds itself -- every poll only returns the *current* line.
Snapshots accumulate in odds_snapshots and get classified as
'opening'/'live'/'closing' based on when they were taken relative to
tip-off, which is what the sharp-money / line-movement analysis in
src/market/ reads from.

Requires ODDS_API_KEY in a local .env (free signup at the-odds-api.com).

NOT YET TESTED against live data -- api.the-odds-api.com is currently
blocked by this environment's egress policy. Response shape below
matches the documented v4 API; verify field names once network access
is available.

500 free requests/month is tight if polling frequently -- each call to
fetch_current_odds() covering the whole NBA slate counts as ONE request
regardless of games returned, so the constraint is how many times per
day you poll, not how many games are on. Plan the polling schedule
(e.g. once at line-open detection, a few times through the day, once
near tip-off) before wiring this into a scheduled job.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from src.db.connection import get_connection

BASE_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
SOURCE_NAME = "odds_api"

# A snapshot taken within this window of tip-off is treated as 'closing'.
CLOSING_WINDOW_MINUTES = 30


def fetch_current_odds(regions="us", markets="h2h,spreads,totals"):
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError("ODDS_API_KEY not set in environment/.env")

    resp = requests.get(
        BASE_URL,
        params={
            "apiKey": api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _classify_line_type(game_id: str, commence_time: datetime, conn) -> str:
    now = datetime.now(timezone.utc)
    minutes_to_tipoff = (commence_time - now).total_seconds() / 60

    if minutes_to_tipoff <= CLOSING_WINDOW_MINUTES:
        return "closing"

    already_seen = conn.execute(
        """SELECT 1 FROM odds_snapshots
           WHERE game_id = ? AND source = ? LIMIT 1""",
        (game_id, SOURCE_NAME),
    ).fetchone()
    return "opening" if not already_seen else "live"


def poll_and_snapshot(db_path=None):
    """Fetch current odds for the full NBA slate and write one
    odds_snapshots row per game per bookmaker. Intended to be called on
    a schedule (see src/cli/daily_pipeline.py) rather than ad hoc.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    events = fetch_current_odds()
    now = datetime.now(timezone.utc).isoformat()

    written = 0
    for event in events:
        commence_time = datetime.fromisoformat(
            event["commence_time"].replace("Z", "+00:00")
        )
        home_team = event["home_team"]
        away_team = event["away_team"]

        game = conn.execute(
            """SELECT game_id FROM games
               WHERE home_team = ? AND away_team = ?
                 AND game_date = ?""",
            (home_team, away_team, commence_time.date().isoformat()),
        ).fetchone()
        if not game:
            continue  # game not yet in our DB from a stats source; skip
        game_id = game["game_id"]
        line_type = _classify_line_type(game_id, commence_time, conn)

        for book in event.get("bookmakers", []):
            home_ml = away_ml = home_spread = total = None
            for market in book.get("markets", []):
                if market["key"] == "h2h":
                    for outcome in market["outcomes"]:
                        if outcome["name"] == home_team:
                            home_ml = outcome["price"]
                        elif outcome["name"] == away_team:
                            away_ml = outcome["price"]
                elif market["key"] == "spreads":
                    for outcome in market["outcomes"]:
                        if outcome["name"] == home_team:
                            home_spread = outcome["point"]
                elif market["key"] == "totals":
                    total = market["outcomes"][0].get("point")

            conn.execute(
                """INSERT INTO odds_snapshots
                   (game_id, source, book, snapshot_time, line_type,
                    home_moneyline, away_moneyline, home_spread, total)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (game_id, SOURCE_NAME, book["key"], now, line_type,
                 home_ml, away_ml, home_spread, total),
            )
            written += 1

    conn.commit()
    conn.close()
    return {"snapshots_written": written}
