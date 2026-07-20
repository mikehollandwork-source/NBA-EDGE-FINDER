"""Ingestion from stats.nba.com via the `nba_api` package.

Primary source for detailed box scores and advanced stats (off/def
rating, pace, possessions) needed for the strength-of-schedule and
Stats-signal calculations.

Confirmed against real nba_api usage across two live backfill attempts:
  1. A full-season LeagueGameFinder call (no date filter, ~1,230 games
     in one response) timed out against nba_api's 30s default
     (requests.exceptions.ReadTimeout after exactly 30s).
  2. Raising the timeout to 90s and adding retries (below) STILL wasn't
     enough for the full-season call -- it timed out on all 3 retries
     (~5 minutes total) on the second attempt. The unfiltered call
     itself is just too heavy for stats.nba.com to serve reliably, not
     a timeout-tuning problem.

Fixed by fetching in <=1-month chunks (persist_season, when no
date_from/date_to is given, chunks the full season internally) instead
of one unfiltered call -- each chunk is far lighter, independently
retried, and a chunk that still fails after retries is skipped (logged)
rather than taking down the whole backfill.

Every endpoint class here takes an explicit `timeout` kwarg (verified
against nba_api's actual __init__ signatures, not assumed) --
NBA_API_TIMEOUT below overrides the 30s default, and _with_retries()
retries transient timeouts/connection errors with backoff.

Column/field names are still otherwise best-effort against nba_api's
documented shapes and may need adjustment once real data comes back.
"""

import datetime as dt
import logging
import time

import pandas as pd
import requests

from src.db.connection import get_connection

REQUEST_DELAY_SECONDS = 0.7
NBA_API_TIMEOUT = 90
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
SOURCE_NAME = "nba_api"

log = logging.getLogger("nba_api_source")


def _num(val):
    """pandas/numpy scalar types (numpy.int64, numpy.float64, ...) --
    what every numeric column of an nba_api DataFrame row actually holds
    -- aren't understood by sqlite3's default type adapter -- it silently
    stores them as a BLOB (via the buffer protocol) instead of an
    INTEGER/REAL, with no error. Caught this via a DB-write smoke test
    against basketball_reference_source.py's parallel code, not assumed
    fine here just because nba_api's own live runs never got far enough
    to surface it. .item() converts a numpy scalar to the equivalent
    native Python type."""
    if pd.isna(val):
        return None
    return val.item() if hasattr(val, "item") else val


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


def _season_date_bounds(season: str) -> tuple[str, str]:
    """NBA season 'YYYY-YY' -> (Oct 1, Jun 30) as MM/DD/YYYY strings."""
    start_year = int(season[:4])
    return f"10/01/{start_year}", f"06/30/{start_year + 1}"


def _month_chunks(date_from: str, date_to: str):
    """Yield (chunk_from, chunk_to) MM/DD/YYYY pairs, each <=1 calendar month."""
    cursor = dt.datetime.strptime(date_from, "%m/%d/%Y")
    end = dt.datetime.strptime(date_to, "%m/%d/%Y")
    while cursor <= end:
        next_month_start = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        chunk_end = min(end, next_month_start - dt.timedelta(days=1))
        yield cursor.strftime("%m/%d/%Y"), chunk_end.strftime("%m/%d/%Y")
        cursor = chunk_end + dt.timedelta(days=1)


def fetch_season_team_log_chunked(season: str, date_from: str = None, date_to: str = None) -> pd.DataFrame:
    """Team game log for the range, fetched in <=1-month chunks rather
    than one unfiltered call -- a full-season LeagueGameFinder call
    proved too heavy for stats.nba.com to reliably serve even with a
    90s timeout and retries (see module docstring). Defaults to the
    full season's Oct-Jun window when no range is given. A chunk that
    still fails after _with_retries()'s attempts is logged and skipped
    -- partial data beats no data for a one-time backfill."""
    if date_from is None and date_to is None:
        date_from, date_to = _season_date_bounds(season)

    frames = []
    for chunk_from, chunk_to in _month_chunks(date_from, date_to):
        try:
            frames.append(fetch_season_team_game_log(season, chunk_from, chunk_to))
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            log.error("nba_api chunk %s-%s failed after retries, skipping: %s",
                     chunk_from, chunk_to, exc)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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
                    _num(row.get("PARTIAL_POSS")), _num(row.get("PLAYER_PTS")),
                    _num(row.get("MATCHUP_FGM")), _num(row.get("MATCHUP_FGA")), now,
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
    window instead. Either way this goes through
    fetch_season_team_log_chunked(), which fetches in <=1-month pieces
    rather than one unfiltered call -- the per-game boxscore calls are
    what actually scale with game volume (traditional + advanced +
    officials = 3 calls per NEW game), the team log itself is now
    chunked specifically to avoid the whole-season timeout.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    team_log = fetch_season_team_log_chunked(season, date_from, date_to)

    if team_log.empty:
        # Every chunk failed (see fetch_season_team_log_chunked) -- an
        # empty DataFrame has no GAME_ID column at all, so groupby()
        # below would raise KeyError rather than just doing nothing.
        conn.close()
        raise RuntimeError("nba_api: every chunk of the team log pull failed, nothing to persist")

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
             _num(home_row.get("PTS")), _num(away_row.get("PTS"))),
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
                    _num(row.get("PTS")), _num(row.get("FGM")), _num(row.get("FGA")),
                    _num(row.get("FG3M")), _num(row.get("FG3A")), _num(row.get("FTM")), _num(row.get("FTA")),
                    _num(row.get("OREB")), _num(row.get("DREB")), _num(row.get("REB")), _num(row.get("AST")),
                    _num(row.get("STL")), _num(row.get("BLK")), _num(row.get("TOV")), _num(row.get("PF")),
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
                    _parse_nba_minutes(row.get("MIN")), _num(row.get("PTS")),
                    _num(row.get("FGM")), _num(row.get("FGA")), _num(row.get("FG3M")), _num(row.get("FG3A")),
                    _num(row.get("FTM")), _num(row.get("FTA")), _num(row.get("OREB")), _num(row.get("DREB")),
                    _num(row.get("REB")), _num(row.get("AST")), _num(row.get("STL")), _num(row.get("BLK")),
                    _num(row.get("TOV" if "TOV" in row else "TO")), _num(row.get("PF")),
                    _num(row.get("PLUS_MINUS")),
                ),
            )

        for _, row in team_adv.iterrows():
            conn.execute(
                """UPDATE team_game_stats SET
                       possessions = ?, off_rating = ?, def_rating = ?,
                       net_rating = ?, pace = ?
                   WHERE game_id = ? AND team = ? AND source = ?""",
                (_num(row.get("POSS")), _num(row.get("OFF_RATING")), _num(row.get("DEF_RATING")),
                 _num(row.get("NET_RATING")), _num(row.get("PACE")),
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
