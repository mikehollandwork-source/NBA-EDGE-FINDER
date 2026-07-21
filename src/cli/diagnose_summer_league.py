"""One-off diagnostic, NOT part of the regular pipeline. Two questions,
both answered against the real APIs rather than assumed:

1. PAGINATION BUG CHECK: every backfill has ingested EXACTLY 100 games
   (balldontlie's max per_page), ending 2025-11-03 -- the suspicious
   signature of reading only the FIRST page. fetch_all_games() paginates
   via meta.next_page, but balldontlie's current API may use cursor
   pagination (meta.next_cursor) instead, which would silently end the
   loop after one page. This prints the real meta object, and directly
   probes whether later-season games exist (Jan 2026 window) -- if they
   do, our "the season has only progressed to Nov 3" belief was wrong
   and the WHOLE season is available once pagination is fixed.

2. SUMMER LEAGUE CHECK: does ESPN's scoreboard carry current July games
   under the NBA slug or a dedicated summer-league slug, and do those
   events have odds? balldontlie/basketball-reference are not expected
   to carry SL at all -- also probed rather than assumed.

python -m src.cli.diagnose_summer_league
"""

import json
import os

import requests

BDL = "https://api.balldontlie.io/v1"
HEADERS_ESPN = {"User-Agent": "nba-edge-finder (personal research)"}


def _bdl_headers():
    key = os.environ.get("BALLDONTLIE_API_KEY")
    return {"Authorization": key} if key else {}


def check_pagination():
    print("=== 1. balldontlie pagination / season-extent check ===")
    r = requests.get(f"{BDL}/games", params={"seasons[]": 2025, "per_page": 100},
                     headers=_bdl_headers(), timeout=20)
    print(f"page 1: status={r.status_code}")
    data = r.json()
    games = data.get("data", [])
    meta = data.get("meta", {})
    print(f"  {len(games)} games on page, meta = {json.dumps(meta)}")
    if games:
        dates = sorted(g["date"][:10] for g in games)
        print(f"  page-1 date range: {dates[0]} .. {dates[-1]}")

    cursor = meta.get("next_cursor")
    if cursor is not None:
        r2 = requests.get(f"{BDL}/games",
                          params={"seasons[]": 2025, "per_page": 100, "cursor": cursor},
                          headers=_bdl_headers(), timeout=20)
        d2 = r2.json()
        g2 = d2.get("data", [])
        print(f"  page 2 via cursor={cursor}: status={r2.status_code}, {len(g2)} games, "
              f"meta={json.dumps(d2.get('meta', {}))}")
        if g2:
            dates2 = sorted(g["date"][:10] for g in g2)
            print(f"  page-2 date range: {dates2[0]} .. {dates2[-1]}")

    # decisive probe: do games from much later in the season exist at all?
    r3 = requests.get(f"{BDL}/games",
                      params={"seasons[]": 2025, "per_page": 25,
                              "start_date": "2026-01-15", "end_date": "2026-01-20"},
                      headers=_bdl_headers(), timeout=20)
    d3 = r3.json()
    g3 = d3.get("data", [])
    print(f"  probe 2026-01-15..20: status={r3.status_code}, {len(g3)} games")
    for g in g3[:5]:
        print(f"    {g['date'][:10]} {g['visitor_team']['abbreviation']} @ "
              f"{g['home_team']['abbreviation']} {g.get('visitor_team_score')}-{g.get('home_team_score')} "
              f"status={g.get('status')}")

    r4 = requests.get(f"{BDL}/games",
                      params={"seasons[]": 2025, "per_page": 25,
                              "start_date": "2026-06-01", "end_date": "2026-06-30"},
                      headers=_bdl_headers(), timeout=20)
    g4 = r4.json().get("data", [])
    print(f"  probe 2026-06 (finals window): {len(g4)} games")
    for g in g4[:6]:
        print(f"    {g['date'][:10]} {g['visitor_team']['abbreviation']} @ "
              f"{g['home_team']['abbreviation']} {g.get('visitor_team_score')}-{g.get('home_team_score')} "
              f"postseason={g.get('postseason')}")


def check_summer_league():
    print("\n=== 2. summer league availability check ===")
    for slug in ("nba", "nba-summer-las-vegas", "nba-summer-league"):
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/{slug}/scoreboard?dates=20260718"
        try:
            r = requests.get(url, headers=HEADERS_ESPN, timeout=20)
            events = r.json().get("events", []) if r.status_code == 200 else []
            print(f"  espn slug {slug!r}: status={r.status_code}, {len(events)} events on 2026-07-18")
            for ev in events[:4]:
                comps = (ev.get("competitions") or [{}])[0]
                has_odds = bool(comps.get("odds"))
                print(f"    {ev.get('name')!r} status={((ev.get('status') or {}).get('type') or {}).get('description')} "
                      f"odds_on_scoreboard={has_odds}")
        except Exception as exc:
            print(f"  espn slug {slug!r}: FAILED {type(exc).__name__}: {exc}")

    # does balldontlie carry July games at all (SL would be season=2026 or postseason flags)?
    for season in (2025, 2026):
        r = requests.get(f"{BDL}/games",
                         params={"seasons[]": season, "per_page": 25,
                                 "start_date": "2026-07-10", "end_date": "2026-07-21"},
                         headers=_bdl_headers(), timeout=20)
        n = len(r.json().get("data", [])) if r.status_code == 200 else f"status={r.status_code}"
        print(f"  balldontlie July 2026 window (season={season}): {n} games")


if __name__ == "__main__":
    check_pagination()
    check_summer_league()
