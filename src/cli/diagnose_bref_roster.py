"""One-off diagnostic, NOT part of the regular pipeline.

The first live run of persist_season_rosters() failed for all 30 teams
with "player_rows=0" -- _extract_player_ids() (which looks for a
data-append-csv attribute, the convention confirmed on box score pages)
found nothing on the roster page, even though pd.read_html found 16-33
real rows in the same table. That means the roster table identifies
players differently than the box score table does -- rather than guess
a second time in the same area (the roster scraper was already flagged
as unverified when written), this fetches one real roster page and
dumps the raw attributes on its first several player rows.

python -m src.cli.diagnose_bref_roster
(defaults to the Lakers; pass --team to check a different bref abbr)
"""

import argparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.basketball-reference.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script; contact via repo owner)"}


def diagnose(bref_abbr: str, season_end_year: int):
    url = f"{BASE_URL}/teams/{bref_abbr}/{season_end_year}.html"
    print(f"Fetching {url}")
    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(f"status={resp.status_code} bytes={len(resp.content)}")
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.find("table", id="roster")
    if table is None:
        print("No table id='roster' found directly in the HTML -- checking comment blocks...")
        from bs4 import Comment
        for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
            if "id=\"roster\"" in c or "id='roster'" in c:
                print("Found a 'roster' table INSIDE an HTML comment block (lazy-loaded).")
                inner = BeautifulSoup(c, "lxml")
                table = inner.find("table", id="roster")
                break
    if table is None:
        print("Still nothing -- dumping all table ids found on the page:")
        for t in soup.find_all("table"):
            print(f"  id={t.get('id')!r}")
        return

    print("\n=== Raw row attributes (first 8 rows of the roster table) ===")
    rows = table.find_all("tr")
    for row in rows[:8]:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        first = cells[0]
        print(f"row: first_cell_text={first.get_text(strip=True)!r} "
              f"first_cell_attrs={dict(first.attrs)} "
              f"row_class={row.get('class')}")
        # also check every cell in the row for any player-link attribute,
        # in case the identifying info isn't on the first cell here
        for c in cells:
            link = c.find("a")
            if link and link.get("href", "").startswith("/players/"):
                print(f"    -> found player link in a <{c.name}> cell: "
                      f"text={link.get_text(strip=True)!r} href={link.get('href')!r} "
                      f"cell_attrs={dict(c.attrs)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", default="LAL", help="bref team abbreviation")
    parser.add_argument("--season-end-year", type=int, default=2026)
    args = parser.parse_args()
    diagnose(args.team, args.season_end_year)
