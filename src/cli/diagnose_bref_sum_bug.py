"""One-off diagnostic, NOT part of the regular pipeline.

The DNP/string-concatenation fix (_read_stat_table coercing stat columns
via pd.to_numeric) passed a local synthetic-HTML test, but a real live
run still produced absurd team totals (e.g. points=221017104938600288 --
the same digit-concatenation signature as the original bug). This fetches
one REAL page that showed the bug and runs the ACTUAL _read_stat_table
against it, printing dtypes and the .sum() result per column, to find
what's different between the synthetic test and real production HTML
rather than guessing a second time.

python -m src.cli.diagnose_bref_sum_bug --game-id 202510220DAL
"""

import argparse

from bs4 import BeautifulSoup

from src.ingestion.basketball_reference_source import _get, _read_stat_table, BASE_URL


def diagnose(game_id: str):
    url = f"{BASE_URL}/boxscores/{game_id}.html"
    print(f"Fetching {url}")
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")

    tables = [t for t in soup.find_all("table") if (t.get("id") or "").endswith("-game-basic")]
    print(f"Found {len(tables)} basic box score tables")

    for table in tables:
        print(f"\n=== {table.get('id')} ===")
        df = _read_stat_table(table)
        print(df.to_string())
        print("\ndtypes:")
        print(df.dtypes)
        for col in df.columns:
            if col in ("Starters", "MP"):
                continue
            print(f"  {col}: sum={df[col].sum()!r} (dtype={df[col].dtype})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-id", required=True, help="BR game id, e.g. 202510220DAL")
    args = parser.parse_args()
    diagnose(args.game_id)
