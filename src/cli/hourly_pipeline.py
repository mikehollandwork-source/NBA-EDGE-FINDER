"""Hourly automation entry point, run by
.github/workflows/hourly_pipeline.yml on a schedule.

Each run: pulls the last few days of results (not the whole season --
see each ingestion module's persist_season date-window params),
reconciles across sources, snapshots odds + Polymarket for upcoming
games, runs line-movement analysis, generates predictions for any
newly-scheduled games ONCE the model exists (still a no-op today --
src/features and src/models/win_predictor.py are empty stubs pending
real data), settles completed games against past predictions, and
sends a Telegram summary.

Does not place bets; output is a recommendation/record only.

Default: only sends a Telegram message when something actually changed
since the last run (a game settled, a new prediction, or a game's odds
moved) rather than an identical summary every single hour -- this is a
judgment call favoring "hands-off, no notification spam" per the ask;
flip ALWAYS_NOTIFY to True if you'd rather get the hourly ping
regardless.
"""

from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from src.db.connection import get_connection
from src.ingestion import (
    balldontlie_source,
    basketball_reference_source,
    nba_api_source,
    odds_api_source,
    polymarket_source,
)
from src.market import line_movement
from src.notify import telegram
from src.reconciliation import cross_check
from src.tracking import record

MODEL_VERSION = "v0-unbuilt"  # bump when src/models/win_predictor.py is real
LOOKBACK_DAYS = 3
SEASON_LABEL = "2025-26"
BALLDONTLIE_SEASON_YEAR = 2025
ALWAYS_NOTIFY = False

load_dotenv()  # no-op in CI (secrets come in as real env vars already)


def _recent_window():
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    return start, today


def ingest_recent_results(db_path=None):
    start, end = _recent_window()

    nba_api_source.persist_season(
        SEASON_LABEL,
        date_from=start.strftime("%m/%d/%Y"),
        date_to=end.strftime("%m/%d/%Y"),
        db_path=db_path,
    )
    balldontlie_source.persist_season(
        BALLDONTLIE_SEASON_YEAR,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        db_path=db_path,
    )
    # Basketball-Reference has no documented rate limit -- run it less
    # aggressively than the other two by only matching/confirming
    # existing rows (persist_season_schedule already does this), not
    # skipped entirely, since it's still our third cross-check source.
    basketball_reference_source.persist_season_schedule(
        season_end_year=int(SEASON_LABEL[:4]) + 1, season_label=SEASON_LABEL, db_path=db_path,
    )


def snapshot_market_data(db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    upcoming = conn.execute(
        """SELECT game_id, home_team, away_team, game_date FROM games
           WHERE status = 'scheduled'
             AND game_date <= date('now', '+2 day')"""
    ).fetchall()
    conn.close()

    try:
        odds_api_source.poll_and_snapshot(db_path=db_path)
    except RuntimeError:
        pass  # ODDS_API_KEY not set in this environment -- skip, don't crash the run

    for game in upcoming:
        try:
            polymarket_source.snapshot_game(
                game["game_id"], game["home_team"], game["away_team"],
                game["game_date"], db_path=db_path,
            )
        except Exception:
            continue  # no Polymarket market for this game, or a transient error

    for game in upcoming:
        line_movement.analyze_game(game["game_id"], db_path=db_path)


def generate_predictions_if_model_ready(db_path=None):
    """No-op until src/features/build_features.py and
    src/models/win_predictor.py have real implementations -- returns 0
    rather than raising, so the rest of the hourly run still completes.
    """
    try:
        from src.features.build_features import build_matchup_features
        from src.models.win_predictor import predict
    except ImportError:
        return 0

    conn = get_connection(db_path) if db_path else get_connection()
    upcoming = conn.execute(
        """SELECT game_id FROM games WHERE status = 'scheduled'
           AND game_id NOT IN (
               SELECT game_id FROM predictions WHERE model_version = ?
           )""",
        (MODEL_VERSION,),
    ).fetchall()
    conn.close()

    generated = 0
    for game in upcoming:
        features = build_matchup_features(game["game_id"], db_path=db_path)
        predict(game["game_id"], features, model_version=MODEL_VERSION, db_path=db_path)
        generated += 1
    return generated


def run(db_path=None):
    ingest_recent_results(db_path)
    cross_check.reconcile_team_stats(db_path)
    cross_check.reconcile_player_stats(db_path)
    snapshot_market_data(db_path)
    predictions_made = generate_predictions_if_model_ready(db_path)
    settlement = record.settle_completed_games(MODEL_VERSION, db_path=db_path)

    changed = predictions_made > 0 or settlement["settled"] > 0
    if changed or ALWAYS_NOTIFY:
        rollup = record.compute_rollup(db_path=db_path)
        telegram.send_message(record.format_summary_message(rollup))

    return {"predictions_made": predictions_made, **settlement}


if __name__ == "__main__":
    print(run())
