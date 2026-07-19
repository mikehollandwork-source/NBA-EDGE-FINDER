"""SQLite connection helper.

Free, zero-setup storage per the agreed architecture. The DB file lives
under data/ (gitignored, regenerable from the free sources) and is
initialized from schema.sql on first use.
"""

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "nba_edge_finder.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the SQLite DB, creating/initializing it from schema.sql if new."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    if is_new:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.commit()

    return conn


def apply_schema(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Re-apply schema.sql (CREATE TABLE IF NOT EXISTS) to an existing DB.

    Safe to run after editing schema.sql to add new tables without
    losing existing data.
    """
    conn = get_connection(db_path)
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
