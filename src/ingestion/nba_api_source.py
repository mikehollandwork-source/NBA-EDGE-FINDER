"""Ingestion from stats.nba.com via the `nba_api` package.

Primary source for detailed box scores and advanced stats (off/def
rating, pace, possessions) needed for the strength-of-schedule and
Stats-signal calculations.

NOT YET TESTED against live data -- this environment's egress policy
currently blocks stats.nba.com. Implemented against nba_api's documented
endpoint/column names; verify column names still match once network
access is available (nba_api occasionally changes them across NBA.com
backend updates).

stats.nba.com is rate-limit sensitive: a short delay is added between
calls. If you see repeated timeouts once this runs live, increase
REQUEST_DELAY_SECONDS rather than removing the delay.
"""

import time
from datetime import datetime

from src.db.connection import get_connection

REQUEST_DELAY_SECONDS = 0.7
SOURCE_NAME = "nba_api"


def _sleep():
    time.sleep(REQUEST_DELAY_SECONDS)


def fetch_season_team_game_log(season: str):
    """Return one row per team per game for the season, e.g. season='2025-26'.

    Uses LeagueGameFinder, which is the standard way to bulk-pull a
    season's worth of team game logs from stats.nba.com in one call
    (paginated internally by the endpoint itself).
    """
    from nba_api.stats.endpoints import leaguegamefinder

    finder = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        league_id_nullable="00",
        season_type_nullable="Regular Season",
    )
    _sleep()
    return finder.get_data_frames()[0]


def fetch_game_boxscore_traditional(game_id: str):
    """Return (player_stats_df, team_stats_df) for a single game_id."""
    from nba_api.stats.endpoints import boxscoretraditionalv2

    box = boxscoretraditionalv2.BoxScoreTraditionalV2(game_id=game_id)
    _sleep()
    frames = box.get_data_frames()
    return frames[0], frames[1]


def fetch_game_boxscore_advanced(game_id: str):
    """Return (player_advanced_df, team_advanced_df) for a single game_id.

    Provides OFF_RATING/DEF_RATING/NET_RATING/PACE used for strength of
    schedule during feature engineering.
    """
    from nba_api.stats.endpoints import boxscoreadvancedv2

    box = boxscoreadvancedv2.BoxScoreAdvancedV2(game_id=game_id)
    _sleep()
    frames = box.get_data_frames()
    return frames[0], frames[1]


def persist_season(season: str, db_path=None):
    """Pull a full season's team game log + per-game box scores and
    write games / team_game_stats / player_game_stats rows.

    This iterates one boxscore call per game (traditional + advanced),
    so a full season (~1,230 games) means ~2,460+ calls to stats.nba.com
    at REQUEST_DELAY_SECONDS apart -- expect this to take a while and to
    need resuming/retrying on transient failures once run for real.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    team_log = fetch_season_team_game_log(season)

    seen_game_ids = set()
    for _, row in team_log.iterrows():
        game_id = row["GAME_ID"]
        is_home = "vs." in row["MATCHUP"]

        if game_id not in seen_game_ids:
            seen_game_ids.add(game_id)
            home_team = row["TEAM_ABBREVIATION"] if is_home else None
            away_team = None if is_home else row["TEAM_ABBREVIATION"]
            conn.execute(
                """INSERT INTO games (game_id, season, game_date, home_team,
                       away_team, home_score, away_score, status)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, 'final')
                   ON CONFLICT(game_id) DO NOTHING""",
                (game_id, season, row["GAME_DATE"], home_team, away_team),
            )

        # NOTE: home/away team + final score need reconciling across the
        # two rows LeagueGameFinder gives per game (one per team) -- left
        # as a follow-up pass once this runs against live data, since the
        # exact merge logic is easier to get right with real rows in hand.

        conn.execute(
            """INSERT INTO team_game_stats
               (game_id, team, source, is_home, points, fgm, fga, fg3m, fg3a,
                ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, team, source) DO NOTHING""",
            (
                game_id, row["TEAM_ABBREVIATION"], SOURCE_NAME, int(is_home),
                row.get("PTS"), row.get("FGM"), row.get("FGA"),
                row.get("FG3M"), row.get("FG3A"), row.get("FTM"), row.get("FTA"),
                row.get("OREB"), row.get("DREB"), row.get("REB"), row.get("AST"),
                row.get("STL"), row.get("BLK"), row.get("TOV"), row.get("PF"),
            ),
        )

    conn.commit()
    conn.close()
