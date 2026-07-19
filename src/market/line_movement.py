"""Sharp-money / line-movement analysis from accumulated odds_snapshots.

Honest limitation (flagged to the user directly): no free source
publishes actual bet-ticket or handle percentages, which is what "public
money %" normally means. Without that, sharp-money detection here is
approximated from line movement alone:

  - reverse line movement (RLM): the line moves AGAINST the side
    presumed to have the public's money. Since we don't have real
    ticket data, "presumed public side" is approximated as the
    opening moneyline favorite (public bettors lean favorites) -- this
    is a real but imperfect proxy, not ground truth.
  - steam move: an unusually large, fast shift between two consecutive
    live snapshots, suggesting synchronized sharp action rather than
    gradual drift.
  - Polymarket gap: Polymarket's price is driven by real money changing
    hands with no bookmaker vig, so a persistent gap between it and the
    de-vigged sportsbook probability is a genuinely independent signal
    (when a Polymarket market exists for the game -- not guaranteed).

These write to line_movement_signals, kept separate from the raw
odds_snapshots so the inference logic can be revised without re-fetching
data.
"""

from datetime import datetime, timezone

from src.db.connection import get_connection
from src.market.vig import compute_vig, remove_vig

STEAM_MOVE_THRESHOLD = 0.05  # 5 percentage points of implied prob, single jump


def analyze_game(game_id: str, db_path=None):
    conn = get_connection(db_path) if db_path else get_connection()

    book_snapshots = conn.execute(
        """SELECT * FROM odds_snapshots
           WHERE game_id = ? AND source IN ('sbr_historical', 'odds_api')
           ORDER BY snapshot_time""",
        (game_id,),
    ).fetchall()

    if len(book_snapshots) < 2:
        conn.close()
        return None  # nothing to compare yet

    opening = book_snapshots[0]
    closing = book_snapshots[-1]

    opening_vig = compute_vig(opening["home_moneyline"], opening["away_moneyline"])
    closing_vig = compute_vig(closing["home_moneyline"], closing["away_moneyline"])

    opening_home_prob, _ = remove_vig(opening["home_moneyline"], opening["away_moneyline"])
    closing_home_prob, _ = remove_vig(closing["home_moneyline"], closing["away_moneyline"])
    line_move_home = closing_home_prob - opening_home_prob

    # Presumed public side = opening favorite (approximation, see module
    # docstring). RLM = line moves away from the favorite despite the
    # assumption that public money keeps piling onto them.
    opening_favorite_is_home = opening_home_prob > 0.5
    reverse_line_movement = (
        (opening_favorite_is_home and line_move_home < 0)
        or (not opening_favorite_is_home and line_move_home > 0)
    )

    steam_move = _detect_steam_move(book_snapshots)

    polymarket_gap = _polymarket_gap(conn, game_id, closing_home_prob)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO line_movement_signals
           (game_id, computed_at, opening_vig, closing_vig,
            line_move_home_cents, reverse_line_movement, steam_move,
            polymarket_vs_book_gap, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (game_id, now, opening_vig, closing_vig, line_move_home,
         int(reverse_line_movement), int(steam_move), polymarket_gap,
         "RLM/public-side is an approximation from opening-favorite, "
         "not real ticket data -- see src/market/line_movement.py"),
    )
    conn.commit()
    conn.close()

    return {
        "opening_vig": opening_vig,
        "closing_vig": closing_vig,
        "line_move_home": line_move_home,
        "reverse_line_movement": reverse_line_movement,
        "steam_move": steam_move,
        "polymarket_gap": polymarket_gap,
    }


def _detect_steam_move(snapshots) -> bool:
    for prev, curr in zip(snapshots, snapshots[1:]):
        if prev["home_moneyline"] is None or curr["home_moneyline"] is None:
            continue
        prev_prob, _ = remove_vig(prev["home_moneyline"], prev["away_moneyline"])
        curr_prob, _ = remove_vig(curr["home_moneyline"], curr["away_moneyline"])
        if abs(curr_prob - prev_prob) >= STEAM_MOVE_THRESHOLD:
            return True
    return False


def _polymarket_gap(conn, game_id: str, book_home_prob: float):
    poly_row = conn.execute(
        """SELECT home_implied_prob FROM odds_snapshots
           WHERE game_id = ? AND source = 'polymarket'
           ORDER BY snapshot_time DESC LIMIT 1""",
        (game_id,),
    ).fetchone()
    if not poly_row or poly_row["home_implied_prob"] is None:
        return None
    return poly_row["home_implied_prob"] - book_home_prob
