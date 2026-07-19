"""Parser for SportsbookReviewsOnline historical NBA odds files.

Used for backtesting: these are the only free source with both
opening AND closing lines already recorded for past games. They are
NOT an API -- you download an .xlsx file by hand per season from
sportsbookreviewsonline.com and place it in data/odds/, then call
load_season_odds() pointing at that file.

NOT YET TESTED against a real file -- sportsbookreviewsonline.com is
blocked by this environment's egress policy so a sample file could not
be downloaded and inspected. The parsing logic below reflects SBR's
long-documented (if quirky) file format:

  - Each game is two consecutive rows: Visitor row, then Home row.
  - Columns include Date, Rot, VH, Team, 1st..4th (quarter scores),
    Final, Open, Close, ML (and sometimes 2H).
  - The 'Open'/'Close' columns hold EITHER a point spread or the game
    total depending on magnitude -- SBR does not label which. The
    accepted heuristic (used by most public parsers of this file) is:
    values with abs() >= 40 are treated as the total, smaller values
    as the spread. This threshold and the exact column names MUST be
    verified against a real downloaded file before trusting output
    from this module.
"""

from datetime import datetime

import pandas as pd

from src.db.connection import get_connection

SOURCE_NAME = "sbr_historical"
SPREAD_VS_TOTAL_THRESHOLD = 40  # abs() values below this = spread, above = total


def _is_total(value) -> bool:
    try:
        return abs(float(value)) >= SPREAD_VS_TOTAL_THRESHOLD
    except (TypeError, ValueError):
        return False


def parse_file(file_path: str) -> pd.DataFrame:
    """Load the raw .xlsx into a DataFrame, one row per team per game
    (mirrors the file's own layout) -- pairing into games happens in
    load_season_odds().
    """
    return pd.read_excel(file_path)


def load_season_odds(file_path: str, season_label: str, db_path=None):
    """Parse an SBR season file and write opening + closing odds_snapshots
    rows, matched to existing games by (date, home_team, away_team).

    Requires games to already exist in the DB (from nba_api/balldontlie
    ingestion) since SBR's own team/game identifiers don't match either
    source's game_id.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    df = parse_file(file_path)

    matched, unmatched = 0, 0
    # Rows come in Visitor/Home pairs.
    for i in range(0, len(df) - 1, 2):
        visitor_row = df.iloc[i]
        home_row = df.iloc[i + 1]

        if visitor_row.get("VH", "V") != "V":
            # Defensive check: file wasn't in the expected V-then-H order
            # for this pair -- skip rather than silently mismatch teams.
            unmatched += 1
            continue

        game_date = _parse_sbr_date(visitor_row["Date"], season_label)
        home_team = str(home_row["Team"]).strip()
        away_team = str(visitor_row["Team"]).strip()

        existing = conn.execute(
            """SELECT game_id FROM games
               WHERE game_date = ? AND home_team = ? AND away_team = ?""",
            (game_date, home_team, away_team),
        ).fetchone()

        if not existing:
            unmatched += 1
            continue

        matched += 1
        game_id = existing["game_id"]

        home_open, home_close = home_row.get("Open"), home_row.get("Close")
        total_open = home_open if _is_total(home_open) else visitor_row.get("Open")
        total_close = home_close if _is_total(home_close) else visitor_row.get("Close")
        home_spread_open = home_open if not _is_total(home_open) else None
        home_spread_close = home_close if not _is_total(home_close) else None

        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO odds_snapshots
               (game_id, source, book, snapshot_time, line_type,
                home_moneyline, away_moneyline, home_spread, total)
               VALUES (?, ?, 'consensus', ?, 'opening', ?, ?, ?, ?)""",
            (game_id, SOURCE_NAME, now, home_row.get("ML"),
             visitor_row.get("ML"), home_spread_open, total_open),
        )
        conn.execute(
            """INSERT INTO odds_snapshots
               (game_id, source, book, snapshot_time, line_type,
                home_moneyline, away_moneyline, home_spread, total)
               VALUES (?, ?, 'consensus', ?, 'closing', ?, ?, ?, ?)""",
            (game_id, SOURCE_NAME, now, home_row.get("ML"),
             visitor_row.get("ML"), home_spread_close, total_close),
        )

    conn.commit()
    conn.close()
    return {"matched": matched, "unmatched": unmatched}


def _parse_sbr_date(raw_date, season_label: str) -> str:
    """SBR dates are like '1019' (Oct 19, no year) or '0322' (Mar 22) --
    the year has to be inferred from the season label (e.g. '2025-26'),
    since games before Jan 1 are the first year and after are the second.
    """
    month = int(str(int(raw_date)).zfill(4)[:2])
    day = int(str(int(raw_date)).zfill(4)[2:])
    start_year, end_year = season_label.split("-")
    year = int(start_year) if month >= 8 else int(start_year[:2] + end_year)
    return datetime(year, month, day).date().isoformat()
