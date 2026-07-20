"""One-off diagnostic, NOT part of the regular pipeline.

Run #3 of the season backtest failed with EVERY nba_api call (11 total,
across 2 different endpoints, 9 of them month-sized chunks and 2 of them
a single league-wide bio-stats call) timing out at the full 90s on every
retry -- while balldontlie and basketball-reference succeeded fine in the
same job. That pattern (100% failure, always a full hang to timeout,
never a fast response of any kind, isolated to one host) looks like
stats.nba.com silently dropping traffic from GitHub Actions' cloud IP
range, not a data-volume/timeout-tuning problem.

This script narrows that down before we commit to redesigning around it:

1. raw_probe() bypasses nba_api entirely and hits stats.nba.com directly
   with a short (15s) timeout and a single attempt. A fast error response
   (403/429/etc) would point at header/bot-detection (fixable). A hang to
   the full 15s with no response at all is more consistent with packets
   being dropped outright (an IP-level block, not easily fixable from
   here).
2. nba_api_single_day_probe() makes the narrowest possible real nba_api
   call (a single day, not a month/season) with only a 20s timeout and no
   retries. If even this hangs the same way, it rules out payload size as
   the cause definitively.

python -m src.cli.diagnose_nba_api
"""

import time

import requests


def raw_probe():
    headers = {
        "Host": "stats.nba.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = "https://stats.nba.com/stats/scoreboardv2"
    params = {"GameDate": "01/15/2026", "LeagueID": "00", "DayOffset": "0"}
    start = time.time()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        elapsed = time.time() - start
        print(f"RAW PROBE: status={resp.status_code} elapsed={elapsed:.1f}s "
              f"bytes={len(resp.content)}")
    except requests.exceptions.RequestException as exc:
        elapsed = time.time() - start
        print(f"RAW PROBE FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")


def nba_api_single_day_probe():
    from nba_api.stats.endpoints import leaguegamefinder

    start = time.time()
    try:
        finder = leaguegamefinder.LeagueGameFinder(
            season_nullable="2025-26",
            league_id_nullable="00",
            season_type_nullable="Regular Season",
            date_from_nullable="01/15/2026",
            date_to_nullable="01/15/2026",
            timeout=20,
        )
        df = finder.get_data_frames()[0]
        elapsed = time.time() - start
        print(f"NBA_API SINGLE-DAY PROBE: rows={len(df)} elapsed={elapsed:.1f}s")
    except Exception as exc:
        elapsed = time.time() - start
        print(f"NBA_API SINGLE-DAY PROBE FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")


def cross_check_probe():
    """Confirm the runner's egress is fine in general (not just bref/bdl
    from the earlier run) by hitting an unrelated, definitely-not-blocked
    host with the same short timeout, for a clean baseline in this same
    run."""
    start = time.time()
    try:
        resp = requests.get("https://www.google.com", timeout=15)
        elapsed = time.time() - start
        print(f"BASELINE PROBE (google.com): status={resp.status_code} elapsed={elapsed:.1f}s")
    except requests.exceptions.RequestException as exc:
        elapsed = time.time() - start
        print(f"BASELINE PROBE FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    print("=== baseline probe (google.com, confirms general egress works) ===")
    cross_check_probe()
    print("=== raw probe (bypass nba_api, direct request to stats.nba.com) ===")
    raw_probe()
    print("=== nba_api probe (single day, narrowest possible real call) ===")
    nba_api_single_day_probe()
