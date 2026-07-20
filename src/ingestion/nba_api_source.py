"""Ingestion from stats.nba.com via the `nba_api` package.

Primary source for detailed box scores and advanced stats (off/def
rating, pace, possessions) needed for the strength-of-schedule and
Stats-signal calculations.

Confirmed against real nba_api usage: a full-season LeagueGameFinder
call (no date filter, ~1,230 games in one response) timed out against
nba_api's 30s default on the first live run of the backfill workflow
(requests.exceptions.ReadTimeout after exactly 30s). Every endpoint
class here takes an explicit `timeout` kwarg (verified against
nba_api's actual __init__ signatures, not assumed) -- NBA_API_TIMEOUT
below overrides the default, and _with_retries() retries transient
timeouts/connection errors with backoff rather than letting one slow
request kill the whole backfill.

Column/field names are still otherwise best-effort against nba_api's
documented shapes and may need adjustment once real data comes back.
"""

import logging
import time

import requests

from src.db.connection import get_connection

REQUEST_DELAY_SECONDS = 0.7
NBA_API_TIMEOUT = 90       # nba_api's own default (30s) proved too short for a
                          # full-season LeagueGameFinder call on the first live run
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
SOURCE_NAME = "nba_api"

log = logging.getLogger("nba_api_source")


def _sleep():
    time.sleep(REQUEST_DELAY_SECONDS)


def _with_retries(build_endpoint):
    """Call build_endpoint() (a zero-arg callable that constructs an
    nba_api endpoint, which fetches on construction) with retries on
    transient network errors -- stats.nba.com is slow/flaky enough that
    a single timeout shouldn't kill an entire season backfill."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return build_endpoint()
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                log.warning("nba_api request timed out (attempt %d/%d), retrying in %ds: %s",
                           attempt, MAX_RETRIES, wait, exc)
                time.sleep(wait)
    raise last_exc


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

    finder = _with_retries(lambda: leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        league_id_nullable="00",
        season_type_nullable="Regular Season",
        date_from_nullable=date_from or "",
        date_to_nullable=date_to or "",
        timeout=NBA_API_TIMEOUT,
    ))
    _sleep()
    return finder.get_data_frames()[0]


def fetch_game_boxscore_traditional(game_id: str):
    """Return (player_stats_df, team_stats_df) for a single game_id."""
    from nba_api.stats.endpoints import boxscoretraditionalv2

    box = _with_retries(lambda: boxscoretraditionalv2.BoxScoreTraditionalV2(
        game_id=game_id, timeout=NBA_API_TIMEOUT))
    _sleep()
    frames = box.get_data_frames()
    return frames[0], frames[1]


def fetch_game_boxscore_advanced(game_id: str):
    """Return (player_advanced_df, team_advanced_df) for a single game_id.

    Provides OFF_RATING/DEF_RATING/NET_RATING/PACE used for strength of
    schedule during feature engineering.
    """
    from nba_api.stats.endpoints import boxscoreadvancedv2

    box = _with_retries(lambda: boxscoreadvancedv2.BoxScoreAdvancedV2(
        game_id=game_id, timeout=NBA_API_TIMEOUT))
    _sleep()
    frames = box.get_data_frames()
    return frames[0], frames[1]


def fetch_boxscore_officials(game_id: str):
    """Officiating crew for a game, e.g. ['Scott Foster', 'Tony Brothers'].
    Empty list on failure -- officials are display/analysis-only, never
    required for the rest of the pipeline to run."""
    from nba_api.stats.endpoints import boxscoresummaryv2

    box = _with_retries(lambda: boxscoresummaryv2.BoxScoreSummaryV2(
        game_id=game_id, timeout=NBA_API_TIMEOUT))
    _sleep()
    officials_df = box.get_data_frames()[2]  # 'Officials' frame
    return [f"{r['FIRST_NAME']} {r['LAST_NAME']}".strip() for _, r in officials_df.iterrows()]


def fetch_player_bio_stats(season: str):
    """Height + position for every active player in one call (season
    bio stats), rather than one commonplayerinfo call per player."""
    from nba_api.stats.endpoints import leaguedashplayerbiostats

    stats = _with_retries(lambda: leaguedashplayerbiostats.LeagueDashPlayerBioStats(
        season=season, timeout=NBA_API_TIMEOUT))
    _sleep()
    return stats.get_data_frames()[0]


def fetch_speed_distance(season: str):
    """League-wide tracking stats (avg speed, distance covered per game)
    for the season, one call. SportVU/tracking data -- coverage can be
    less consistent than regular box score stats; treat as optional."""
    from nba_api.stats.endpoints import leaguedashptstats

    stats = _with_retries(lambda: leaguedashptstats.LeagueDashPtStats(
        season=season, pt_measure_type="SpeedDistance", player_or_team="Player",
        timeout=NBA_API_TIMEOUT,
    ))
    _sleep()
    return stats.get_data_frames()[0]


def fetch_matchups(def_player_id: str, season: str):
    """Real player-vs-player matchup data (time matched up, points/FG
    allowed) for everyone a given defender has guarded this season --
    the PRIMARY h2h signal per the design, when the sample is big enough
    to trust; undocumented endpoint, verify shape on first live run."""
    from nba_api.stats.endpoints import leagueseasonmatchups

    matchups = _with_retries(lambda: leagueseasonmatchups.LeagueSeasonMatchups(
        season=season, def_player_id_nullable=def_player_id, timeout=NBA_API_TIMEOUT,
    ))
    _sleep()
    return matchups.get_data_frames()[0]


def persist_matchups_for_defenders(defender_ids: list[str], season: str, db_path=None):
    """Fetch + store real h2h matchup data for a specific set of defenders
    (e.g. tonight's projected starters), not the whole league -- one API
    call per defender, so this is meant to be called selectively per
    upcoming matchup rather than as a league-wide nightly job."""
    conn = get_connection(db_path) if db_path else get_connection()
    now = _now_iso()
    for def_id in defender_ids:
        try:
            rows = fetch_matchups(def_id, season)
        except Exception as exc:
            import logging
            logging.getLogger("nba_api_source").warning(
                "matchups fetch failed for defender %s: %s", def_id, exc)
            continue
        for _, row in rows.iterrows():
            conn.execute(
                """INSERT INTO player_matchups
                   (def_player_id, off_player_id, season, partial_possessions,
                    points_allowed, fg_made_allowed, fg_attempted_allowed, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(def_player_id, off_player_id, season) DO UPDATE SET
                       partial_possessions = excluded.partial_possessions,
                       points_allowed = excluded.points_allowed,
                       fg_made_allowed = excluded.fg_made_allowed,
                       fg_attempted_allowed = excluded.fg_attempted_allowed,
                       updated_at = excluded.updated_at""",
                (
                    def_id, str(row.get("OFF_PLAYER_ID")), season,
                    row.get("PARTIAL_POSS"), row.get("PLAYER_PTS"),
                    row.get("MATCHUP_FGM"), row.get("MATCHUP_FGA"), now,
                ),
            )
    conn.commit()
    conn.close()


def persist_player_bio(season: str, db_path=None):
    """Write position/height into the players reference table -- refresh
    periodically (not every hourly run), positions/heights don't change
    mid-season."""
    conn = get_connection(db_path) if db_path else get_connection()
    bio = fetch_player_bio_stats(season)
    now = _now_iso()
    for _, row in bio.iterrows():
        conn.execute(
            """INSERT INTO players
               (player_id, player_name, position, height_inches, current_team, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(player_id) DO UPDATE SET
                   player_name = excluded.player_name,
                   position = excluded.position,
                   height_inches = excluded.height_inches,
                   current_team = excluded.current_team,
                   updated_at = excluded.updated_at""",
            (
                str(row["PLAYER_ID"]), row.get("PLAYER_NAME"),
                _bucket_position(row.get("POSITION")),
                _height_to_inches(row.get("PLAYER_HEIGHT")),
                row.get("TEAM_ABBREVIATION"), SOURCE_NAME, now,
            ),
        )
    conn.commit()
    conn.close()


def _bucket_position(raw_position) -> str | None:
    """stats.nba.com POSITION is free text like 'Guard', 'Forward-Center'
    -- bucket to our PG/SG/SF/PF/C convention as best-effort (it doesn't
    distinguish PG/SG or SF/PF on its own, so this is a coarse guess;
    good enough as the FALLBACK positional bucket, with real h2h matchup
    data as primary per the design)."""
    if not raw_position:
        return None
    p = str(raw_position).lower()
    if "guard" in p and "forward" not in p:
        return "SG"  # coarse: nba_api doesn't split PG/SG
    if "forward" in p and "center" not in p and "guard" not in p:
        return "SF"  # coarse: nba_api doesn't split SF/PF
    if "center" in p:
        return "C"
    if "guard" in p and "forward" in p:
        return "SG"
    if "forward" in p and "center" in p:
        return "PF"
    return None


def _height_to_inches(height_str) -> float | None:
    """stats.nba.com height is 'FT-IN' e.g. '6-9' -> 81.0."""
    if not height_str or "-" not in str(height_str):
        return None
    try:
        ft, inch = str(height_str).split("-")
        return int(ft) * 12 + int(inch)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def persist_season(season: str, date_from: str = None, date_to: str = None, db_path=None):
    """Pull team game logs (optionally limited to a date window) and
    write games / team_game_stats rows, plus per-game player box scores
    (traditional + advanced) and officials for any game not already
    fully populated.

    Full-season calls (~1,230 games) are meant for a one-time backfill;
    the hourly job should pass date_from/date_to for a short recent
    window instead, since LeagueGameFinder itself is one call regardless
    of range -- it's the per-game boxscore calls that scale with volume
    (traditional + advanced + officials = 3 calls per NEW game).
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

        already_had_players = conn.execute(
            "SELECT 1 FROM player_game_stats WHERE game_id = ? AND source = ? LIMIT 1",
            (game_id, SOURCE_NAME),
        ).fetchone()

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

        if already_had_players:
            continue  # already pulled the expensive per-game data for this one

        _persist_game_detail(conn, game_id, home_row["TEAM_ABBREVIATION"],
                             away_row["TEAM_ABBREVIATION"])

    conn.commit()
    conn.close()


def _persist_game_detail(conn, game_id: str, home_team: str, away_team: str):
    """Per-game player box score (traditional + advanced merged) and
    officials -- the 3 extra calls per new game. Any failure here is
    logged-and-skipped, not fatal to the season pull."""
    try:
        player_trad, _team_trad = fetch_game_boxscore_traditional(game_id)
        player_adv, team_adv = fetch_game_boxscore_advanced(game_id)
        adv_by_player = {str(r["PLAYER_ID"]): r for _, r in player_adv.iterrows()}

        for _, row in player_trad.iterrows():
            team_abbr = row["TEAM_ABBREVIATION"]
            is_home = int(team_abbr == home_team)
            adv = adv_by_player.get(str(row["PLAYER_ID"]), {})
            conn.execute(
                """INSERT INTO player_game_stats
                   (game_id, player_id, player_name, team, source, is_home,
                    is_starter, status, minutes, points, fgm, fga, fg3m, fg3a,
                    ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf, plus_minus)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(game_id, player_id, source) DO NOTHING""",
                (
                    game_id, str(row["PLAYER_ID"]), row.get("PLAYER_NAME"),
                    team_abbr, SOURCE_NAME, is_home,
                    int(bool(row.get("START_POSITION"))),
                    "dnp" if row.get("COMMENT") and not row.get("MIN") else "active",
                    _parse_nba_minutes(row.get("MIN")), row.get("PTS"),
                    row.get("FGM"), row.get("FGA"), row.get("FG3M"), row.get("FG3A"),
                    row.get("FTM"), row.get("FTA"), row.get("OREB"), row.get("DREB"),
                    row.get("REB"), row.get("AST"), row.get("STL"), row.get("BLK"),
                    row.get("TOV" if "TOV" in row else "TO"), row.get("PF"),
                    row.get("PLUS_MINUS"),
                ),
            )

        for _, row in team_adv.iterrows():
            conn.execute(
                """UPDATE team_game_stats SET
                       possessions = ?, off_rating = ?, def_rating = ?,
                       net_rating = ?, pace = ?
                   WHERE game_id = ? AND team = ? AND source = ?""",
                (row.get("POSS"), row.get("OFF_RATING"), row.get("DEF_RATING"),
                 row.get("NET_RATING"), row.get("PACE"),
                 game_id, row["TEAM_ABBREVIATION"], SOURCE_NAME),
            )

        for name in fetch_boxscore_officials(game_id):
            conn.execute(
                """INSERT INTO game_officials (game_id, official_name)
                   VALUES (?, ?) ON CONFLICT(game_id, official_name) DO NOTHING""",
                (game_id, name),
            )
    except Exception as exc:
        import logging
        logging.getLogger("nba_api_source").warning(
            "per-game detail fetch failed for %s: %s", game_id, exc)


def _parse_nba_minutes(min_str) -> float | None:
    """nba_api MIN is 'MM:SS' string; convert to float minutes."""
    if not min_str:
        return None
    if ":" in str(min_str):
        m, s = str(min_str).split(":")
        return int(m) + int(s) / 60
    try:
        return float(min_str)
    except (ValueError, TypeError):
        return None
