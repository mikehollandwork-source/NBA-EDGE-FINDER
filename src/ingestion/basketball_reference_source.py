"""Ingestion from Basketball-Reference via scraping (no official API).

Used for historical depth and advanced metrics, and as a third
cross-check source. No official rate limit is published; be
conservative (delay between requests, and cache pages locally if this
starts running frequently) to avoid getting blocked.

NOT YET TESTED against live data -- basketball-reference.com is
currently blocked by this environment's egress policy. Table structure
below (ids 'schedule', 'tgl_basic') reflects the site's known layout,
but Basketball-Reference does change table ids/columns occasionally --
verify once network access is available.

IMPORTANT cross-source caveat: Basketball-Reference has its own game
ids (date + home team, e.g. '202602010LAL') and its own team
abbreviations that differ from nba.com's for a few teams (BRK vs BKN,
PHO vs PHX, CHO vs CHA). There is no shared game_id across sources, so
reconciliation must match games by (date, home_team, away_team) after
normalizing abbreviations through TEAM_ABBR_TO_NBA below, not by id.
"""

import time

import pandas as pd
import requests

from src.db.connection import get_connection

BASE_URL = "https://www.basketball-reference.com"
REQUEST_DELAY_SECONDS = 2.0  # conservative: no official rate limit published
SOURCE_NAME = "bref"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script; contact via repo owner)"}

# Only the abbreviations that actually differ from nba.com's.
TEAM_ABBR_TO_NBA = {
    "BRK": "BKN",
    "PHO": "PHX",
    "CHO": "CHA",
}

SEASON_MONTHS = [
    "october", "november", "december", "january", "february",
    "march", "april", "may", "june",
]


def _get(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.text


def fetch_month_schedule(season_end_year: int, month: str) -> pd.DataFrame:
    """One month's games, e.g. season_end_year=2026, month='october'.

    Returns empty DataFrame (not an error) if that month has no page,
    since not every season plays in every listed month.
    """
    url = f"{BASE_URL}/leagues/NBA_{season_end_year}_games-{month}.html"
    try:
        html = _get(url)
    except requests.HTTPError:
        return pd.DataFrame()
    tables = pd.read_html(html, attrs={"id": "schedule"})
    return tables[0] if tables else pd.DataFrame()


def fetch_full_season_schedule(season_end_year: int) -> pd.DataFrame:
    """Concatenate every month's schedule for the season, e.g. 2026 = 2025-26."""
    frames = [fetch_month_schedule(season_end_year, m) for m in SEASON_MONTHS]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_team_game_log(team_abbr_bref: str, season_end_year: int) -> pd.DataFrame:
    """Team's full game log (basic stats) for the season."""
    url = f"{BASE_URL}/teams/{team_abbr_bref}/{season_end_year}/gamelog/"
    html = _get(url)
    tables = pd.read_html(html, attrs={"id": "tgl_basic"})
    return tables[0] if tables else pd.DataFrame()


def _normalize_abbr(bref_abbr: str) -> str:
    return TEAM_ABBR_TO_NBA.get(bref_abbr, bref_abbr)


def persist_season_schedule(season_end_year: int, season_label: str, db_path=None):
    """Write games rows from the BR schedule, matched to existing rows by
    (date, home_team, away_team) rather than inserting a new game_id --
    BR doesn't share nba.com's id scheme, so this only fills in/confirms
    games that were already inserted by another source. Run nba_api or
    balldontlie ingestion first.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    schedule = fetch_full_season_schedule(season_end_year)

    matched, unmatched = 0, 0
    for _, row in schedule.iterrows():
        game_date = pd.to_datetime(row["Date"]).date().isoformat()
        home_team = _normalize_abbr(row["Home/Neutral"])
        away_team = _normalize_abbr(row["Visitor/Neutral"])

        existing = conn.execute(
            """SELECT game_id FROM games
               WHERE game_date = ? AND home_team = ? AND away_team = ?""",
            (game_date, home_team, away_team),
        ).fetchone()

        if existing:
            matched += 1
            game_id = existing["game_id"]
            conn.execute(
                """INSERT INTO team_game_stats
                   (game_id, team, source, is_home, points)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(game_id, team, source) DO NOTHING""",
                (game_id, home_team, SOURCE_NAME, row.get("PTS.1")),
            )
            conn.execute(
                """INSERT INTO team_game_stats
                   (game_id, team, source, is_home, points)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(game_id, team, source) DO NOTHING""",
                (game_id, away_team, SOURCE_NAME, row.get("PTS")),
            )
        else:
            # No matching game from another source yet -- logged, not
            # inserted, since we have no game_id to assign it here.
            unmatched += 1

    conn.commit()
    conn.close()
    return {"matched": matched, "unmatched": unmatched}
