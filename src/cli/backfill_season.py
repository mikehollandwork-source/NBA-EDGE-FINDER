"""One-time full-season backfill -- run this before the first backtest,
since src/cli/hourly_pipeline.py only ever pulls a rolling few-day
window by design (a full season on every hourly run would be far too
slow/wasteful). Not scheduled; run manually or from the on-demand
backtest workflow.

python -m src.cli.backfill_season --season 2025-26 --balldontlie-year 2025
"""

from __future__ import annotations

import argparse
import glob
import logging

from dotenv import load_dotenv

from src.ingestion import (
    balldontlie_source,
    basketball_reference_source,
    nba_api_source,
    sbr_historical_odds,
)
from src.reconciliation import cross_check

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_season")

load_dotenv()


def run(season: str, balldontlie_year: int, db_path=None):
    log.info("Backfilling full season %s (this is the slow, one-time pull)", season)

    log.info("nba_api: team game log + per-game box scores + officials...")
    nba_api_source.persist_season(season, db_path=db_path)

    log.info("balldontlie: games + player box scores (cross-check source)...")
    balldontlie_source.persist_season(balldontlie_year, db_path=db_path)

    log.info("basketball-reference: schedule confirmation (third cross-check)...")
    basketball_reference_source.persist_season_schedule(
        season_end_year=int(season[:4]) + 1, season_label=season, db_path=db_path,
    )

    log.info("nba_api: player bio (position/height reference)...")
    nba_api_source.persist_player_bio(season, db_path=db_path)

    log.info("Cross-checking sources...")
    cross_check.reconcile_team_stats(db_path)
    cross_check.reconcile_player_stats(db_path)

    _load_sbr_odds_if_present(season, db_path)

    log.info("Backfill complete.")


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
    args = parser.parse_args()
    run(args.season, args.balldontlie_year)


if __name__ == "__main__":
    main()
