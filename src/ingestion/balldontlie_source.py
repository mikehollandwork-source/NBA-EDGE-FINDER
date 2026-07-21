"""Ingestion from the free balldontlie.io REST API.

Used as a cross-check source for games/scores/basic box scores against
nba_api and Basketball-Reference.

NOTE on the host/auth: the api.balldontlie.io/v1 host + Authorization-
header key was confirmed correct on the first live backfill -- the
/games endpoint works fine. HOWEVER /stats returned 401 Unauthorized on
that same run with the same key, which /games accepted -- since the key
itself is clearly valid (it worked elsewhere), this looks like the free
tier not including per-game player stats rather than an auth format
bug. Not confirmed against balldontlie's pricing docs (no network
access from this session) -- if a paid tier turns out to be required,
persist_season() below still gets games/scores from the free tier and
degrades player stats to "skipped" rather than aborting.
"""

import logging
import os
import time

import requests

from src.db.connection import get_connection

BASE_URL = "https://api.balldontlie.io/v1"
REQUEST_DELAY_SECONDS = 0.5
SOURCE_NAME = "balldontlie"

log = logging.getLogger("balldontlie_source")


def _headers():
    api_key = os.environ.get("BALLDONTLIE_API_KEY")
    return {"Authorization": api_key} if api_key else {}


def fetch_games(season: int, cursor: int = None, per_page: int = 100,
                 start_date: str = None, end_date: str = None):
    """Return one page of games for a season, e.g. season=2025 for 2025-26.

    balldontlie uses CURSOR pagination now, not page numbers -- the meta
    block carries {"next_cursor": <int>, "per_page": N} with NO
    "next_page" field (verified live via diagnose_summer_league.py).
    Pass the previous page's meta.next_cursor to get the next page.

    start_date/end_date ('YYYY-MM-DD') limit to a recent window instead
    of the whole season -- what the hourly job uses.
    """
    params = {"seasons[]": season, "per_page": per_page}
    if cursor is not None:
        params["cursor"] = cursor
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    resp = requests.get(
        f"{BASE_URL}/games", params=params, headers=_headers(), timeout=15,
    )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def fetch_all_games(season: int, start_date: str = None, end_date: str = None):
    """Paginate through fetch_games() via cursor and return all matching
    games.

    This previously followed meta.next_page, a field balldontlie's API no
    longer returns -- so the loop always stopped after page 1, silently
    capping every full-season pull at the first 100 games (Oct 21 - Nov 3
    only). Confirmed and fixed against the real cursor-based meta shape.
    The MAX_PAGES cap is a safety valve: a full season is ~13 pages of
    100, so 100 pages is far more than enough while still guaranteeing
    termination if a malformed response ever kept returning a cursor."""
    MAX_PAGES = 100
    games = []
    cursor = None
    for _ in range(MAX_PAGES):
        data = fetch_games(season, cursor=cursor, start_date=start_date, end_date=end_date)
        games.extend(data["data"])
        cursor = data.get("meta", {}).get("next_cursor")
        if cursor is None:
            break
    else:
        log.warning("balldontlie fetch_all_games hit MAX_PAGES=%d -- "
                    "results may be truncated", MAX_PAGES)
    return games


def fetch_game_stats(game_id: int):
    """Return per-player stat lines for a single game (basic box score)."""
    resp = requests.get(
        f"{BASE_URL}/stats",
        params={"game_ids[]": game_id, "per_page": 100},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()["data"]


def persist_season(season: int, start_date: str = None, end_date: str = None, db_path=None):
    """Pull games + player box scores (optionally limited to a recent
    date window) and write games / player_game_stats rows.

    Commits after each game's row (not just once at the end) so partial
    progress survives if a later game's request fails -- the first live
    run lost every game it had already fetched because the whole
    function only committed once, after an uncaught exception on a
    later game's /stats call.

    If /stats comes back 401/403 (see module docstring -- looks like a
    tier restriction, not a bad key), player stats are skipped for the
    rest of THIS call rather than retried per game and logged once, not
    once per game -- games/scores still get written either way.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    games = fetch_all_games(season, start_date=start_date, end_date=end_date)

    stats_available = True
    stats_skipped = 0

    for g in games:
        home_team = g["home_team"]["abbreviation"]
        away_team = g["visitor_team"]["abbreviation"]
        game_date = g["date"][:10]  # API returns a full timestamp

        # balldontlie's own numeric id is NOT the same id nba_api uses --
        # match against an existing row by (date, home_team, away_team)
        # first; only mint a new (prefixed, so it can't collide with
        # nba.com's own id format) game_id if nothing else created this
        # game yet.
        existing = conn.execute(
            """SELECT game_id FROM games
               WHERE game_date = ? AND home_team = ? AND away_team = ?""",
            (game_date, home_team, away_team),
        ).fetchone()
        game_id = existing["game_id"] if existing else f"bdl_{g['id']}"

        conn.execute(
            """INSERT INTO games (game_id, season, game_date, home_team,
                   away_team, home_score, away_score, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id) DO UPDATE SET
                   home_score = excluded.home_score,
                   away_score = excluded.away_score,
                   status = excluded.status""",
            (
                game_id, f"{season}-{str(season + 1)[-2:]}", game_date,
                home_team, away_team, g.get("home_team_score"),
                g.get("visitor_team_score"),
                "final" if g.get("status") == "Final" else "scheduled",
            ),
        )

        conn.commit()  # games row survives even if this game's stats call fails below

        if stats_available:
            try:
                stats = fetch_game_stats(g["id"])
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    log.warning(
                        "balldontlie /stats returned %s (games endpoint works fine with "
                        "the same key) -- likely a free-tier restriction, not a bad key. "
                        "Skipping player stats for the rest of this run.", status,
                    )
                    stats_available = False
                else:
                    log.warning("balldontlie stats fetch failed for game %s: %s", g["id"], exc)
                stats_skipped += 1
                continue
        else:
            stats_skipped += 1
            continue

        for s in stats:
            team_abbr = s["team"]["abbreviation"]
            is_home = team_abbr == home_team
            conn.execute(
                """INSERT INTO player_game_stats
                   (game_id, player_id, player_name, team, source, is_home,
                    minutes, points, fgm, fga, fg3m, fg3a, ftm, fta,
                    oreb, dreb, reb, ast, stl, blk, tov, pf)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(game_id, player_id, source) DO NOTHING""",
                (
                    game_id, str(s["player"]["id"]),
                    f"{s['player']['first_name']} {s['player']['last_name']}",
                    team_abbr, SOURCE_NAME, int(is_home),
                    _parse_minutes(s.get("min")), s.get("pts"),
                    s.get("fgm"), s.get("fga"), s.get("fg3m"), s.get("fg3a"),
                    s.get("ftm"), s.get("fta"), s.get("oreb"), s.get("dreb"),
                    s.get("reb"), s.get("ast"), s.get("stl"), s.get("blk"),
                    s.get("turnover"), s.get("pf"),
                ),
            )
        conn.commit()

    conn.close()
    if stats_skipped:
        log.info("balldontlie: skipped player stats for %d/%d game(s)", stats_skipped, len(games))


def _parse_minutes(min_str):
    """balldontlie returns minutes as 'MM:SS' or 'MM' string; convert to float."""
    if not min_str:
        return None
    if ":" in str(min_str):
        m, s = str(min_str).split(":")
        return int(m) + int(s) / 60
    try:
        return float(min_str)
    except ValueError:
        return None
