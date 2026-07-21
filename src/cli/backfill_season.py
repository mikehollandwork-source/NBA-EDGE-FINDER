"""One-time full-season backfill -- run this before the first backtest,
since src/cli/hourly_pipeline.py only ever pulls a rolling few-day
window by design (a full season on every hourly run would be far too
slow/wasteful). Not scheduled; run manually or from the on-demand
backtest workflow.

python -m src.cli.backfill_season --season 2025-26 --balldontlie-year 2025

PRIMARY SOURCE: stats.nba.com is confirmed unreachable from GitHub
Actions (silently blocks/drops traffic from its cloud IP range -- see
nba_api_source.py's module docstring and the diagnose_nba_api.py probe
results). balldontlie now discovers games/schedule (the primary game_id
assigner), and basketball-reference supplies the actual per-game box
scores, advanced stats, officials, and positions -- every
build_features.py query filters on source='bref'. nba_api is best-effort
and OFF by default (pass --include-nba-api only when running this from a
machine with a non-cloud IP, e.g. locally, to also pull nba_api's richer
data -- real player h2h matchups, precise advanced ratings, speed/
distance tracking -- for a fuller one-off backtest dataset).
"""

from __future__ import annotations

import argparse
import glob
import logging

from dotenv import load_dotenv

from src.ingestion import (
    balldontlie_source,
    basketball_reference_source,
    espn_source,
    nba_api_source,
    sbr_historical_odds,
)
from src.reconciliation import cross_check

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_season")

load_dotenv()


def run(season: str, balldontlie_year: int, db_path=None, include_nba_api: bool = False):
    """Each step is wrapped so one source's failure (even after its own
    internal retries) doesn't take down the whole backfill -- matches
    the "never let one failure abort the whole run" convention used
    throughout this project's ingestion code. The one exception:
    basketball-reference's box scores are the PRIMARY source every
    downstream feature/signal query filters on (source = 'bref'), so if
    that fails entirely there's nothing for the backtest to work with
    regardless of what else succeeds -- that's reported clearly at the
    end, not hidden."""
    log.info("Backfilling full season %s (this is the slow, one-time pull)", season)

    log.info("balldontlie: games + schedule (primary game/schedule discoverer)...")
    _step("balldontlie season pull", balldontlie_source.persist_season, balldontlie_year, db_path=db_path)

    log.info("basketball-reference: schedule confirmation (cross-check)...")
    _step("basketball-reference schedule", basketball_reference_source.persist_season_schedule,
         season_end_year=int(season[:4]) + 1, season_label=season, db_path=db_path)

    log.info("basketball-reference: per-game box scores + advanced stats + officials (PRIMARY)...")
    bref_ok = _step("basketball-reference box scores", basketball_reference_source.persist_season_boxscores,
                    season, db_path=db_path)

    log.info("basketball-reference: rosters (position/height reference)...")
    _step("basketball-reference rosters", basketball_reference_source.persist_season_rosters,
         season_end_year=int(season[:4]) + 1, db_path=db_path)

    if include_nba_api:
        log.info("nba_api: team game log + per-game box scores + officials (best-effort -- "
                "confirmed unreachable from GitHub Actions, only useful run from a non-cloud IP)...")
        _step("nba_api season pull", nba_api_source.persist_season, season, db_path=db_path)
        log.info("nba_api: player bio (position/height reference)...")
        _step("nba_api player bio", nba_api_source.persist_player_bio, season, db_path=db_path)

    log.info("espn: historical opening/closing odds for completed games (for backtest ROI)...")
    _step("espn historical odds backfill", espn_source.backfill_closing_odds, season, db_path=db_path)

    log.info("Cross-checking sources...")
    _step("reconcile team stats", cross_check.reconcile_team_stats, db_path)
    _step("reconcile player stats", cross_check.reconcile_player_stats, db_path)

    _load_sbr_odds_if_present(season, db_path)

    if bref_ok:
        log.info("Backfill complete.")
    else:
        log.error(
            "Backfill finished but the PRIMARY basketball-reference box score pull "
            "failed entirely -- the backtest has nothing to work with regardless of "
            "what else succeeded (every feature query filters on source='bref'). "
            "Re-run this step."
        )
        raise SystemExit(1)


def _step(name: str, fn, *args, **kwargs) -> bool:
    """Run one backfill step; log and return False on failure instead of
    propagating, so later steps still get a chance to run."""
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:
        log.error("%s failed: %s", name, exc)
        return False


def _load_sbr_odds_if_present(season: str, db_path=None):
    """SBR historical odds are a manual download, not an API -- load
    whatever's already been placed in data/odds/ (any .xlsx file),
    skip with a clear message if nothing's there yet. Without this,
    the backtest still runs and produces accuracy, just no ROI."""
    candidates = glob.glob("data/odds/*.xlsx")
    if not candidates:
        log.warning(
            "No SBR historical odds file found in data/odds/ -- backtest will "
            "still compute accuracy but NOT ROI. Download the %s season file "
            "from sportsbookreviewsonline.com, commit it to data/odds/, and "
            "re-run backfill to get real ROI numbers.", season,
        )
        return
    for path in candidates:
        log.info("Loading SBR historical odds from %s", path)
        result = sbr_historical_odds.load_season_odds(path, season, db_path=db_path)
        log.info("SBR odds: matched %d game(s), %d unmatched", result["matched"], result["unmatched"])


def main():
    parser = argparse.ArgumentParser(description="One-time full-season backfill")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--balldontlie-year", type=int, default=2025)
    parser.add_argument("--include-nba-api", action="store_true",
                        help="Also pull nba_api's richer stats (real player h2h "
                             "matchups, precise advanced ratings, speed/distance "
                             "tracking) -- only useful when run from a machine with "
                             "a non-cloud IP; stats.nba.com is confirmed unreachable "
                             "from GitHub Actions.")
    args = parser.parse_args()
    run(args.season, args.balldontlie_year, include_nba_api=args.include_nba_api)


if __name__ == "__main__":
    main()
