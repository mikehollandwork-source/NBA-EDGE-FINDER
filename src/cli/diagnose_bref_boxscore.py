"""One-off diagnostic, NOT part of the regular pipeline.

Before building a real basketball-reference per-game player box score
scraper (needed now that nba_api is confirmed unreachable from GitHub
Actions -- see diagnose_nba_api.py), check what a real BR box score page
actually looks like. BR's table structure/ids are well-known from public
documentation but NOT verified against this project's actual live
requests -- and BR is also known for hiding some tables inside HTML
comments (lazy-loaded client-side) on certain page types, which plain
pd.read_html misses entirely. Rather than guess and find out via a failed
scraper later, this fetches one real recent game and reports:

- every table id found directly in the raw HTML
- every table id found ONLY inside HTML comments (the lazy-load pattern)
- whether an officials list is present, and what the surrounding text
  looks like, so the real scraper can target it precisely

python -m src.cli.diagnose_bref_boxscore
(auto-picks a real, already-played game from the schedule; pass
--game-id to check a specific one instead, format is
YYYYMMDD0<home team's 3-letter bref abbreviation>)
"""

import argparse
import re

import pandas as pd
import requests
from bs4 import BeautifulSoup, Comment

from src.ingestion.basketball_reference_source import TEAM_ABBR_TO_NBA, fetch_month_schedule
from src.ingestion.nba_teams import name_to_abbr

BASE_URL = "https://www.basketball-reference.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script; contact via repo owner)"}

# TEAM_ABBR_TO_NBA maps bref's abbr -> nba.com's for the 3 that differ
# (BRK->BKN, PHO->PHX, CHO->CHA); invert it to go the other direction.
NBA_TO_BREF_ABBR = {nba: bref for bref, nba in TEAM_ABBR_TO_NBA.items()}


def pick_real_game_id() -> str:
    """Find one real, already-completed game from the schedule (proven
    reachable in the live backtest run) rather than guessing a date/team
    combo -- BR's box score game id is YYYYMMDD0<home team's OWN bref
    abbreviation>. The schedule table's Home/Neutral column is a full
    team name ("Brooklyn Nets"), not an abbreviation -- first bug found
    here: assumed it was already an abbreviation, it isn't."""
    for month in ("january", "december", "november", "october"):
        sched = fetch_month_schedule(2026, month)
        if sched.empty:
            continue
        # "PTS.1" is the home team's score in BR's schedule table (see
        # persist_season_schedule's own use of PTS vs PTS.1) -- NaN means
        # the game hasn't been played yet.
        played = sched[sched["PTS.1"].notna()] if "PTS.1" in sched.columns else sched
        if played.empty:
            continue
        row = played.iloc[0]
        game_date = pd.to_datetime(row["Date"]).strftime("%Y%m%d")
        home_name = row["Home/Neutral"]
        nba_abbr = name_to_abbr(home_name)
        if not nba_abbr:
            raise RuntimeError(f"Couldn't resolve team name {home_name!r} to an abbreviation")
        bref_abbr = NBA_TO_BREF_ABBR.get(nba_abbr, nba_abbr)
        return f"{game_date}0{bref_abbr}"
    raise RuntimeError("Couldn't find a played game in any checked month's schedule")


def diagnose(game_id: str):
    url = f"{BASE_URL}/boxscores/{game_id}.html"
    print(f"Fetching {url}")
    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(f"status={resp.status_code} bytes={len(resp.content)}")
    resp.raise_for_status()
    html = resp.text

    soup = BeautifulSoup(html, "lxml")

    direct_tables = soup.find_all("table")
    print(f"\n=== Tables directly in HTML ({len(direct_tables)}) ===")
    for t in direct_tables:
        print(f"  id={t.get('id')!r}")

    comments = soup.find_all(string=lambda s: isinstance(s, Comment))
    print(f"\n=== Checking {len(comments)} HTML comment blocks for hidden tables ===")
    hidden_table_ids = []
    for c in comments:
        if "<table" in c:
            inner = BeautifulSoup(c, "lxml")
            for t in inner.find_all("table"):
                hidden_table_ids.append(t.get("id"))
    print(f"Hidden (comment-wrapped) table ids found: {hidden_table_ids}")

    print("\n=== Officials search ===")
    officials_match = re.search(r"Officials:(.*?)(?:<br|</div|\n)", html, re.IGNORECASE)
    if officials_match:
        print(f"Regex match near 'Officials:': {officials_match.group(0)[:300]}")
    else:
        print("No 'Officials:' text found via simple regex -- inspect manually.")
    officials_div = soup.find(string=re.compile("Officials", re.IGNORECASE))
    if officials_div:
        parent = officials_div.find_parent()
        print(f"Found 'Officials' text node, parent tag={parent.name if parent else None}, "
              f"parent text={parent.get_text(strip=True)[:300] if parent else None}")

    print("\n=== Sample of a basic box score table (first team found) ===")
    basic_tables = [t for t in direct_tables if t.get("id", "").endswith("-game-basic")]
    if basic_tables:
        import io
        df = pd.read_html(io.StringIO(str(basic_tables[0])))[0]
        print(df.columns.tolist())
        print(df.head(3))
    else:
        print("No direct '-game-basic' table found -- likely inside a comment block, see above.")

    print("\n=== Sample of an advanced box score table (first team found) ===")
    adv_tables = [t for t in direct_tables if t.get("id", "").endswith("-game-advanced")]
    if adv_tables:
        import io
        df = pd.read_html(io.StringIO(str(adv_tables[0])))[0]
        print(df.columns.tolist())
        print(df.head(3))
    else:
        print("No direct '-game-advanced' table found -- likely inside a comment block, see above.")

    print("\n=== Response encoding check (mangled names bug hunt) ===")
    print(f"resp.encoding (requests' guess) = {resp.encoding!r}")
    print(f"resp.apparent_encoding (chardet's guess) = {resp.apparent_encoding!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-id", default=None,
                        help="BR game id, e.g. 202601150LAL (date + home team bref abbr). "
                             "Auto-picks a real played game from the schedule if omitted.")
    args = parser.parse_args()
    game_id = args.game_id or pick_real_game_id()
    if not args.game_id:
        print(f"Auto-picked game id from schedule: {game_id}")
    diagnose(game_id)
