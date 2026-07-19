"""Settles completed games against our predictions and rolls up the
units/win-loss record for the Telegram report.

Default staking: flat 1 unit per pick, settled against the closing
moneyline available at settlement time. Both are defaults, not fixed
requirements -- revisit if you want edge-sized (e.g. Kelly) staking, or
to settle at whatever price was live when the prediction was actually
generated rather than the eventual close.
"""

from datetime import datetime, timedelta, timezone

from src.db.connection import get_connection
from src.market.vig import american_to_implied_prob

DEFAULT_UNITS_RISKED = 1.0


def settle_completed_games(model_version: str, db_path=None):
    """Find predicted games that have finished but aren't in bet_record
    yet, determine win/loss, and compute units_won from the closing
    moneyline. Safe to call every run -- already-settled games are
    skipped via the UNIQUE(game_id, model_version) constraint.
    """
    conn = get_connection(db_path) if db_path else get_connection()

    rows = conn.execute(
        """SELECT p.game_id, p.predicted_winner, p.model_version,
                  g.game_date, g.home_team, g.away_team, g.home_score,
                  g.away_score, g.status
           FROM predictions p
           JOIN games g ON g.game_id = p.game_id
           WHERE p.model_version = ? AND g.status = 'final'
             AND NOT EXISTS (
                 SELECT 1 FROM bet_record b
                 WHERE b.game_id = p.game_id AND b.model_version = p.model_version
             )""",
        (model_version,),
    ).fetchall()

    settled = 0
    for row in rows:
        actual_winner = (
            row["home_team"] if row["home_score"] > row["away_score"] else row["away_team"]
        )
        won = actual_winner == row["predicted_winner"]

        closing = conn.execute(
            """SELECT home_moneyline, away_moneyline FROM odds_snapshots
               WHERE game_id = ? AND line_type = 'closing'
                 AND home_moneyline IS NOT NULL
               ORDER BY snapshot_time DESC LIMIT 1""",
            (row["game_id"],),
        ).fetchone()

        units_won = None
        if closing:
            picked_ml = (
                closing["home_moneyline"]
                if row["predicted_winner"] == row["home_team"]
                else closing["away_moneyline"]
            )
            units_won = (
                _payout_units(picked_ml, DEFAULT_UNITS_RISKED) if won
                else -DEFAULT_UNITS_RISKED
            )

        conn.execute(
            """INSERT INTO bet_record
               (game_id, game_date, model_version, predicted_winner,
                actual_winner, result, units_risked, units_won, settled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, model_version) DO NOTHING""",
            (row["game_id"], row["game_date"], model_version,
             row["predicted_winner"], actual_winner,
             "win" if won else "loss", DEFAULT_UNITS_RISKED, units_won,
             datetime.now(timezone.utc).isoformat()),
        )
        settled += 1

    conn.commit()
    conn.close()
    return {"settled": settled}


def _payout_units(american_odds: int, units_risked: float) -> float:
    """Profit in units for a winning bet at the given American odds
    (does not include the stake itself, matching standard unit-tracking
    convention: +1.91u profit on a -110 winner, not +2.91u).
    """
    if american_odds is None:
        return 0.0
    if american_odds > 0:
        return units_risked * (american_odds / 100)
    return units_risked * (100 / -american_odds)


def compute_rollup(db_path=None, as_of: datetime = None):
    """Units/win-loss record for today, yesterday, this week, this
    month, and year-to-date, keyed off game_date (not settled_at) so a
    game counts toward the day it was actually played.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    as_of = as_of or datetime.now(timezone.utc)
    today = as_of.date()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    periods = {
        "Today": today.isoformat(),
        "Yesterday": yesterday.isoformat(),
        "This week": week_start.isoformat(),
        "This month": month_start.isoformat(),
        "YTD": year_start.isoformat(),
    }

    rollup = {}
    for label, since in periods.items():
        end_date = (
            yesterday.isoformat() if label == "Yesterday" else today.isoformat()
        )
        row = conn.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE result = 'win') AS wins,
                   COUNT(*) FILTER (WHERE result = 'loss') AS losses,
                   COALESCE(SUM(units_won), 0) AS units
               FROM bet_record
               WHERE game_date BETWEEN ? AND ?""",
            (since, end_date),
        ).fetchone()
        rollup[label] = {
            "wins": row["wins"], "losses": row["losses"], "units": row["units"],
        }

    conn.close()
    return rollup


def format_summary_message(rollup: dict) -> str:
    lines = ["*NBA Edge Finder -- Hourly Update*", ""]
    for label, r in rollup.items():
        sign = "+" if r["units"] >= 0 else ""
        lines.append(f"{label}: {r['wins']}-{r['losses']}  ({sign}{r['units']:.2f}u)")
    return "\n".join(lines)
