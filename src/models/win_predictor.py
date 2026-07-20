"""Ties src/models/signals.py to the predictions table -- the interface
src/cli/hourly_pipeline.py calls.

The regression half of the win condition is refit from every completed
game before the prediction's date, then cached per (as_of date, db) for
the rest of that process's run -- refitting per-game would mean
rebuilding features for the entire season on every single prediction,
which does not scale. A new process (the next hourly run) refits fresh,
which is what gives the "rolling/expanding walk-forward" re-discovery
the design calls for: the win condition is never fit on data it will
later be tested against.

Known performance caveat, flagged rather than silently accepted: fitting
uses build_matchup_features() for every prior game, and consistency
scoring calls it again for each team's last 4 games -- correct per the
point-in-time design, but not fast. If a full-season backtest run turns
out to be too slow in practice, the fix is to cache flattened diffs per
(game_id, as_of) in a table instead of recomputing, not to loosen the
point-in-time discipline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.db.connection import get_connection
from src.features import build_features as bf
from src.models import signals as sig

DEFAULT_FEATURE_NAMES = [n for names in sig.FEATURE_GROUPS.values() for n in names]

_MODEL_CACHE: dict[tuple, object] = {}


def _get_or_fit_model(as_of: str, feature_names: list[str], db_path=None):
    cache_key = (str(db_path), tuple(feature_names), as_of)
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]

    conn = get_connection(db_path) if db_path else get_connection()
    rows = conn.execute(
        """SELECT game_id, home_score, away_score, game_date FROM games
           WHERE status = 'final' AND game_date < ? ORDER BY game_date""",
        (as_of,),
    ).fetchall()
    conn.close()

    samples = []
    for g in rows:
        try:
            feats = bf.build_matchup_features(g["game_id"], db_path=db_path, as_of=g["game_date"])
        except Exception:
            continue
        diffs = sig.flatten_features(feats)
        samples.append((diffs, g["home_score"] > g["away_score"]))

    model = sig.fit_win_condition_weights(samples, feature_names)
    _MODEL_CACHE[cache_key] = model
    return model


def predict(game_id: str, features: dict | None = None, model_version: str = "v1",
           feature_names: list[str] | None = None, db_path=None) -> dict:
    """Compute signals for one game and write a predictions row."""
    feature_names = feature_names or DEFAULT_FEATURE_NAMES
    feats = features or bf.build_matchup_features(game_id, db_path=db_path)
    model = _get_or_fit_model(feats["as_of"], feature_names, db_path)

    result = sig.compute_signals(
        game_id, features=feats, feature_names=feature_names,
        regression_model=model, db_path=db_path,
    )

    conn = get_connection(db_path) if db_path else get_connection()
    conn.execute(
        """INSERT INTO predictions
           (game_id, model_version, generated_at, home_stats_signal, away_stats_signal,
            home_consistency_signal, away_consistency_signal, home_composite_score,
            away_composite_score, edge, predicted_winner)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (game_id, model_version, datetime.now(timezone.utc).isoformat(),
         result["home_stats_signal"], result["away_stats_signal"],
         result["home_consistency_signal"], result["away_consistency_signal"],
         result["home_composite_score"], result["away_composite_score"],
         result["edge"], result["predicted_winner"]),
    )
    conn.commit()
    conn.close()
    return result
