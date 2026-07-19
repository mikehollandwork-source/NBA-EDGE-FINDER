"""Cross-checks the same game/stat across sources instead of trusting one.

Any disagreement (e.g. nba_api and balldontlie report different points
for the same team in the same game) gets written to reconciliation_log
rather than silently auto-resolved, so it stays visible and reviewable
before feature engineering runs on top of it.

Policy on disagreement (confirm/revisit with user once real mismatches
show up): nba_api is treated as the tiebreaker source when two sources
disagree, since it's the NBA's own data -- but the mismatch is always
logged either way, and feature engineering should be able to flag/skip
games with unresolved conflicts rather than pick silently.
"""

from datetime import datetime, timezone

from src.db.connection import get_connection

# Only compare fields present across all three sources' inserts today;
# expand as more fields get populated per-source.
COMPARABLE_TEAM_FIELDS = ["points"]
COMPARABLE_PLAYER_FIELDS = ["minutes", "points"]

TIEBREAKER_SOURCE = "nba_api"


def reconcile_team_stats(db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    now = datetime.now(timezone.utc).isoformat()
    mismatches = 0

    rows = conn.execute(
        "SELECT DISTINCT game_id, team FROM team_game_stats"
    ).fetchall()

    for row in rows:
        game_id, team = row["game_id"], row["team"]
        by_source = {
            r["source"]: r
            for r in conn.execute(
                "SELECT * FROM team_game_stats WHERE game_id = ? AND team = ?",
                (game_id, team),
            ).fetchall()
        }
        if len(by_source) < 2:
            continue  # nothing to cross-check yet

        sources = list(by_source.keys())
        for field in COMPARABLE_TEAM_FIELDS:
            values = {s: by_source[s][field] for s in sources}
            distinct_values = {v for v in values.values() if v is not None}
            if len(distinct_values) > 1:
                mismatches += 1
                source_a, source_b = sources[0], sources[1]
                conn.execute(
                    """INSERT INTO reconciliation_log
                       (game_id, field, source_a, value_a, source_b, value_b, detected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (game_id, f"team_game_stats.{field}", source_a,
                     str(values[source_a]), source_b, str(values[source_b]), now),
                )

    conn.commit()
    conn.close()
    return {"mismatches_logged": mismatches}


def reconcile_player_stats(db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()
    now = datetime.now(timezone.utc).isoformat()
    mismatches = 0

    rows = conn.execute(
        "SELECT DISTINCT game_id, player_id FROM player_game_stats"
    ).fetchall()

    for row in rows:
        game_id, player_id = row["game_id"], row["player_id"]
        by_source = {
            r["source"]: r
            for r in conn.execute(
                "SELECT * FROM player_game_stats WHERE game_id = ? AND player_id = ?",
                (game_id, player_id),
            ).fetchall()
        }
        if len(by_source) < 2:
            continue

        sources = list(by_source.keys())
        for field in COMPARABLE_PLAYER_FIELDS:
            values = {s: by_source[s][field] for s in sources}
            distinct_values = {v for v in values.values() if v is not None}
            if len(distinct_values) > 1:
                mismatches += 1
                source_a, source_b = sources[0], sources[1]
                conn.execute(
                    """INSERT INTO reconciliation_log
                       (game_id, field, source_a, value_a, source_b, value_b, detected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (game_id, f"player_game_stats.{field}", source_a,
                     str(values[source_a]), source_b, str(values[source_b]), now),
                )

    conn.commit()
    conn.close()
    return {"mismatches_logged": mismatches}


def unresolved_conflicts(db_path=None):
    """Games with a logged mismatch, for manual review or for feature
    engineering to exclude/flag until resolved.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    rows = conn.execute(
        "SELECT DISTINCT game_id FROM reconciliation_log"
    ).fetchall()
    conn.close()
    return [r["game_id"] for r in rows]
