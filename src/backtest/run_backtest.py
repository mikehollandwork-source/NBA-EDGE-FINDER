"""Walk-forward backtest against a season, with combination-testing
across different stat combinations (src/models/signals.py's FEATURE_GROUPS)
-- what the user asked for: "test them with different stat combinations
too find consistent winning plays."

For each combo: walks the season chronologically in weekly chunks. Before
each chunk, the win-condition regression is refit using only games
already final before that chunk (expanding window -- never sees the
future), predictions are generated for every game in the chunk, and each
settled against the actual result + the SBR historical closing line for
real ROI (flat 1 unit/pick, same convention as src/tracking/record.py).
After the full season, accuracy/ROI/games-graded are written to
backtest_runs, one row per combo, so combos are directly comparable.

Known performance caveat (see win_predictor.py's docstring for the same
note): build_matchup_features + team_consistency recompute a lot per
game, correctly per the point-in-time design but not fast. A full
1,230-game season across several combos may take a while -- this is
meant to run via the on-demand GitHub Actions workflow
(.github/workflows/backtest.yml), not interactively.

NOT YET RUN against real data -- needs live network access (see README)
plus a downloaded SBR historical odds file loaded via
src.ingestion.sbr_historical_odds before this can produce real numbers.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from src.db.connection import get_connection
from src.features import build_features as bf
from src.models import signals as sig
from src.tracking.record import _payout_units

CHUNK_DAYS = 7

# A curated set of combinations to compare, rather than an unbounded
# combinatorial search over 2^10 group subsets -- each one answers a
# specific question about which signal groups are actually pulling
# weight. Add/edit entries here to test something else.
CURATED_COMBOS: dict[str, list[str]] = {
    "team_only": ["team"],
    "team_sos": ["team", "sos", "home_away_split"],
    "team_playstyle": ["team", "sos", "home_away_split", "playstyle", "style_clash"],
    "team_situational": ["team", "sos", "home_away_split", "rest_travel", "schedule_spot"],
    "positions_heavy": ["team", "sos", "positions", "bench"],
    "full": sig.ALL_FEATURE_GROUPS,
}


def _season_games(conn, season: str, start_date: str = None, end_date: str = None):
    query = "SELECT * FROM games WHERE season = ? AND status = 'final'"
    params = [season]
    if start_date:
        query += " AND game_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND game_date <= ?"
        params.append(end_date)
    query += " ORDER BY game_date"
    return conn.execute(query, params).fetchall()


def _closing_moneyline(conn, game_id: str, team: str, home_team: str):
    row = conn.execute(
        """SELECT home_moneyline, away_moneyline FROM odds_snapshots
           WHERE game_id = ? AND line_type = 'closing' AND home_moneyline IS NOT NULL
           ORDER BY snapshot_time DESC LIMIT 1""",
        (game_id,),
    ).fetchone()
    if not row:
        return None
    return row["home_moneyline"] if team == home_team else row["away_moneyline"]


def _chunk_ranges(games, chunk_days: int = CHUNK_DAYS):
    if not games:
        return
    start = datetime.strptime(games[0]["game_date"], "%Y-%m-%d")
    end = datetime.strptime(games[-1]["game_date"], "%Y-%m-%d")
    cursor = start
    while cursor <= end:
        chunk_end = cursor + timedelta(days=chunk_days - 1)
        yield cursor.date().isoformat(), chunk_end.date().isoformat()
        cursor = chunk_end + timedelta(days=1)


def run_combo(combo_name: str, feature_groups: list[str], season: str, db_path=None) -> dict:
    conn = get_connection(db_path) if db_path else get_connection()
    feature_names = [n for g in feature_groups for n in sig.FEATURE_GROUPS.get(g, [])]
    games = _season_games(conn, season)

    predictions, correct, settled, total_units = [], 0, 0, 0.0
    model_cache: dict[str, object] = {}

    for chunk_start, chunk_end in _chunk_ranges(games):
        # Expanding window: fit on every final game strictly before this
        # chunk. Cache per chunk_start so we don't refit once per game.
        if chunk_start not in model_cache:
            train_games = conn.execute(
                """SELECT game_id, home_score, away_score, game_date FROM games
                   WHERE status = 'final' AND game_date < ? ORDER BY game_date""",
                (chunk_start,),
            ).fetchall()
            samples = []
            for g in train_games:
                try:
                    feats = bf.build_matchup_features(g["game_id"], db_path=db_path, as_of=g["game_date"])
                except Exception:
                    continue
                diffs = sig.flatten_features(feats, groups=feature_groups)
                samples.append((diffs, g["home_score"] > g["away_score"]))
            model_cache[chunk_start] = sig.fit_win_condition_weights(samples, feature_names)

        model = model_cache[chunk_start]
        chunk_games = [g for g in games if chunk_start <= g["game_date"] <= chunk_end]

        for g in chunk_games:
            try:
                result = sig.compute_signals(
                    g["game_id"], feature_names=feature_names, regression_model=model, db_path=db_path,
                )
            except Exception:
                continue

            actual_winner = g["home_team"] if g["home_score"] > g["away_score"] else g["away_team"]
            correct += int(result["predicted_winner"] == actual_winner)

            ml = _closing_moneyline(conn, g["game_id"], result["predicted_winner"], g["home_team"])
            units = None
            if ml is not None:
                won = result["predicted_winner"] == actual_winner
                units = _payout_units(ml, 1.0) if won else -1.0
                total_units += units
                settled += 1

            predictions.append({
                "game_id": g["game_id"], "game_date": g["game_date"],
                "predicted_winner": result["predicted_winner"], "actual_winner": actual_winner,
                "edge": result["edge"], "units": units,
            })

    conn.close()
    graded = len(predictions)
    return {
        "combo": combo_name, "groups": feature_groups, "season": season,
        "games_graded": graded, "correct": correct,
        "accuracy": (correct / graded) if graded else None,
        "bets_settled": settled, "roi_units": total_units,
        "roi_pct": (total_units / settled) if settled else None,
        "predictions": predictions,
    }


def run_all_combos(season: str, combos: dict[str, list[str]] = None, db_path=None) -> list[dict]:
    combos = combos or CURATED_COMBOS
    conn = get_connection(db_path) if db_path else get_connection()
    now = datetime.now(timezone.utc).isoformat()

    results = []
    for name, groups in combos.items():
        result = run_combo(name, groups, season, db_path=db_path)
        results.append(result)
        conn.execute(
            """INSERT INTO backtest_runs
               (model_version, season, run_at, accuracy, roi, calibration_error, notes)
               VALUES (?, ?, ?, ?, ?, NULL, ?)""",
            (f"combo:{name}", season, now, result["accuracy"], result["roi_pct"],
             json.dumps({"groups": groups, "games_graded": result["games_graded"],
                        "bets_settled": result["bets_settled"]})),
        )
    conn.commit()
    conn.close()

    results.sort(key=lambda r: (r["roi_pct"] if r["roi_pct"] is not None else -999), reverse=True)
    return results


def main():
    parser = argparse.ArgumentParser(description="Walk-forward backtest with combination-testing")
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--out", default="data/backtest_results.json")
    args = parser.parse_args()

    results = run_all_combos(args.season)
    summary = [{k: v for k, v in r.items() if k != "predictions"} for r in results]
    print(json.dumps(summary, indent=2))

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull results (including per-game predictions) written to {args.out}")


if __name__ == "__main__":
    main()
