"""Ingestion from the free balldontlie.io REST API.

Used as a cross-check source for games/scores/basic box scores against
nba_api and Basketball-Reference.

NOT YET TESTED against live data -- balldontlie.io is currently blocked
by this environment's egress policy.

NOTE on the host/auth: balldontlie has changed its API shape over time.
As of this writing the documented free-tier host is api.balldontlie.io/v1
with an API key (free signup) sent as an Authorization header -- NOT the
earlier no-auth www.balldontlie.io/api/v1 host some older tutorials
reference. Verify this against https://docs.balldontlie.io once network
access is available; BALLDONTLIE_API_KEY should be set in .env either way
since the free tier still requires a key.
"""

import os
import time

import requests

from src.db.connection import get_connection

BASE_URL = "https://api.balldontlie.io/v1"
REQUEST_DELAY_SECONDS = 0.5
SOURCE_NAME = "balldontlie"


def _headers():
    api_key = os.environ.get("BALLDONTLIE_API_KEY")
    return {"Authorization": api_key} if api_key else {}


def fetch_games(season: int, page: int = 1, per_page: int = 100):
    """Return one page of games for a season, e.g. season=2025 for 2025-26."""
    resp = requests.get(
        f"{BASE_URL}/games",
        params={"seasons[]": season, "page": page, "per_page": per_page},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.json()


def fetch_all_games(season: int):
    """Paginate through fetch_games() and return the full season's games."""
    games = []
    page = 1
    while True:
        data = fetch_games(season, page=page)
        games.extend(data["data"])
        meta = data.get("meta", {})
        if not meta.get("next_page"):
            break
        page = meta["next_page"]
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


def persist_season(season: int, db_path=None):
    """Pull a season's games + player box scores and write games /
    team_game_stats (aggregated from player rows) / player_game_stats.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    games = fetch_all_games(season)

    for g in games:
        game_id = str(g["id"])
        home_team = g["home_team"]["abbreviation"]
        away_team = g["visitor_team"]["abbreviation"]
        conn.execute(
            """INSERT INTO games (game_id, season, game_date, home_team,
                   away_team, home_score, away_score, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id) DO NOTHING""",
            (
                game_id, f"{season}-{str(season + 1)[-2:]}", g["date"],
                home_team, away_team, g.get("home_team_score"),
                g.get("visitor_team_score"),
                "final" if g.get("status") == "Final" else "scheduled",
            ),
        )

        stats = fetch_game_stats(g["id"])
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
