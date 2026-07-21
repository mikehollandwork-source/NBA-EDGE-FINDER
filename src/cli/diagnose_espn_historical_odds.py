"""One-off diagnostic, NOT part of the regular pipeline.

SBR's historical odds archive is behind anti-bot protection, so the
backtest still has no historical opening/closing lines. Candidate
replacement: ESPN's per-event odds endpoint (the same one
espn_source.lines() already uses for LIVE games) is keyed by event id,
so it may well still serve odds for COMPLETED games -- and possibly
with explicit open/close blocks per team. If that holds for this
season's already-played games, the backtest can backfill real
opening/closing moneylines from ESPN and SBR stops mattering entirely.

This fetches one PAST date's scoreboard, then dumps the full raw
structure of the odds endpoint for the first few games: every provider
item, and for each, exactly which fields exist (open/close/current per
team, moneyline values, provider name). Raw JSON keys are printed
rather than assumed -- per this project's standing rule after repeated
"documented shape != real shape" bugs.

python -m src.cli.diagnose_espn_historical_odds --date 2025-10-22
"""

import argparse
import json

import requests

HEADERS = {"User-Agent": "nba-edge-finder (personal research)"}
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"
EVENT_ODDS = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
              "events/{eid}/competitions/{eid}/odds")


def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    print(f"GET {url} -> {r.status_code} ({len(r.content)} bytes)")
    r.raise_for_status()
    return r.json()


def _describe_team_odds(label, t):
    if not isinstance(t, dict):
        print(f"      {label}: MISSING")
        return
    print(f"      {label}: keys={sorted(t.keys())}")
    for phase in ("open", "close", "current"):
        block = t.get(phase)
        if isinstance(block, dict):
            ml = block.get("moneyLine")
            print(f"        {phase}: keys={sorted(block.keys())} moneyLine={ml!r}")
    if "moneyLine" in t:
        print(f"        flat moneyLine={t['moneyLine']!r}")


def diagnose(date: str):
    data = _get(SCOREBOARD.format(date.replace("-", "")))
    events = data.get("events", [])
    print(f"\n{len(events)} events on {date}")

    for ev in events[:3]:
        eid = ev.get("id")
        name = ev.get("name")
        status = ((ev.get("status") or {}).get("type") or {}).get("description")
        print(f"\n=== event {eid}: {name} (status={status}) ===")
        try:
            odds = _get(EVENT_ODDS.format(eid=eid))
        except requests.exceptions.RequestException as exc:
            print(f"  odds endpoint FAILED: {exc}")
            continue
        items = odds.get("items") or []
        print(f"  {len(items)} odds item(s)")
        for i, it in enumerate(items):
            provider = (it.get("provider") or {}).get("name")
            print(f"  --- item {i}: provider={provider!r} keys={sorted(it.keys())}")
            _describe_team_odds("homeTeamOdds", it.get("homeTeamOdds"))
            _describe_team_odds("awayTeamOdds", it.get("awayTeamOdds"))
            for field in ("moneylineWinner", "spread", "overUnder", "details"):
                if field in it:
                    print(f"      {field}={it[field]!r}")
        if items:
            print("  full raw first item (truncated to 3000 chars):")
            print(json.dumps(items[0], indent=2)[:3000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2025-10-22",
                        help="A past date with completed games, YYYY-MM-DD")
    args = parser.parse_args()
    diagnose(args.date)
