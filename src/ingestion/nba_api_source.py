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

from src.db.connection import get_connection

REQUEST_DELAY_SECONDS = 0.7
SOURCE_NAME = "nba_api"


def _sleep():
    time.sleep(REQUEST_DELAY_SECONDS)


def fetch_season_team_game_log(season: str, date_from: str = None, date_to: str = None):
    """Return one row per team per game for the season, e.g. season='2025-26'.

    date_from/date_to are 'MM/DD/YYYY' strings (nba_api/stats.nba.com's
    expected format) -- pass both to limit to a recent window instead of
    pulling the whole season, which is what the hourly job does.

    Uses LeagueGameFinder, which is the standard way to bulk-pull a
    season's worth of team game logs from stats.nba.com in one call
    (paginated internally by the endpoint itself).
    """
    from nba_api.stats.endpoints import leaguegamefinder

    finder = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        league_id_nullable="00",
        season_type_nullable="Regular Season",
        date_from_nullable=date_from or "",
        date_to_nullable=date_to or "",
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


def persist_season(season: str, date_from: str = None, date_to: str = None, db_path=None):
    """Pull team game logs (optionally limited to a date window) and
    write games / team_game_stats rows.

    Full-season calls (~1,230 games) are meant for a one-time backfill;
    the hourly job should pass date_from/date_to for a short recent
    window instead, since LeagueGameFinder itself is one call regardless
    of range -- it's the per-game boxscore calls that scale with volume.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    team_log = fetch_season_team_game_log(season, date_from, date_to)

    # LeagueGameFinder gives one row per team per game (two rows share a
    # game_id) -- group them so home/away and both scores can be set
    # together rather than inserting a half-populated game row per row.
    for game_id, game_rows in team_log.groupby("GAME_ID"):
        if len(game_rows) != 2:
            continue  # incomplete pair (e.g. postponed game); skip for now

        home_row = next(r for _, r in game_rows.iterrows() if "vs." in r["MATCHUP"])
        away_row = next(r for _, r in game_rows.iterrows() if "@" in r["MATCHUP"])

        conn.execute(
            """INSERT INTO games (game_id, season, game_date, home_team,
                   away_team, home_score, away_score, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'final')
               ON CONFLICT(game_id) DO UPDATE SET
                   home_score = excluded.home_score,
                   away_score = excluded.away_score,
                   status = excluded.status""",
            (game_id, season, home_row["GAME_DATE"],
             home_row["TEAM_ABBREVIATION"], away_row["TEAM_ABBREVIATION"],
             home_row.get("PTS"), away_row.get("PTS")),
        )

        for row, is_home in ((home_row, 1), (away_row, 0)):
            conn.execute(
                """INSERT INTO team_game_stats
                   (game_id, team, source, is_home, points, fgm, fga, fg3m, fg3a,
                    ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(game_id, team, source) DO NOTHING""",
                (
                    game_id, row["TEAM_ABBREVIATION"], SOURCE_NAME, is_home,
                    row.get("PTS"), row.get("FGM"), row.get("FGA"),
                    row.get("FG3M"), row.get("FG3A"), row.get("FTM"), row.get("FTA"),
                    row.get("OREB"), row.get("DREB"), row.get("REB"), row.get("AST"),
                    row.get("STL"), row.get("BLK"), row.get("TOV"), row.get("PF"),
                ),
            )

    conn.commit()
    conn.close()
