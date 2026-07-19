"""Wikipedia pageviews as a public-ATTENTION signal, ported directly from
the MLB repo's wiki.py -- the Wikimedia pageviews API is sport-agnostic,
so this only needed the team-name-to-article mapping swapped for NBA.

Official Wikimedia REST API, no auth, full history (so unlike consensus/
forum signals this one is backtestable). Point-in-time: the window ends
the day BEFORE the game, no lookahead. Fails soft: a missing article /
network error just drops that team.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import urllib.parse

import requests

log = logging.getLogger("wiki_source")

API = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
       "en.wikipedia/all-access/all-agents/{article}/daily/{start}/{end}")
TIMEOUT = 20
ATTENTION_DAYS = 3
POLITE_DELAY = 0.2
PREFETCH_DAYS = 130

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "nba-edge-finder/1.0 "
                        "(personal betting research)"})

# NBA team names that don't map directly to their Wikipedia article title.
SPECIAL: dict[str, str] = {}

_SERIES: dict[str, dict[str, int]] = {}
_COVERED: dict[str, tuple[str, str]] = {}


def _article(team_name: str) -> str:
    title = SPECIAL.get(team_name, team_name).replace(" ", "_")
    return urllib.parse.quote(title, safe="")


def _fetch_series(article: str, start: str, end: str) -> dict[str, int] | None:
    try:
        r = SESSION.get(API.format(article=article, start=start, end=end), timeout=TIMEOUT)
        time.sleep(POLITE_DELAY)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return {it["timestamp"][:8]: int(it.get("views", 0) or 0)
                for it in r.json().get("items", [])}
    except Exception as exc:
        log.warning("wiki pageviews failed for %s: %s", article, exc)
        return None


def _ensure_series(article: str, start: dt.date, end: dt.date) -> None:
    s8, e8 = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    cov = _COVERED.get(article)
    if cov and cov[0] <= s8 and cov[1] >= e8:
        return
    fstart = min(start, end - dt.timedelta(days=PREFETCH_DAYS))
    if cov:
        fstart = min(fstart, dt.datetime.strptime(cov[0], "%Y%m%d").date())
        end = max(end, dt.datetime.strptime(cov[1], "%Y%m%d").date())
    series = _fetch_series(article, fstart.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if series is None:
        return
    _SERIES.setdefault(article, {}).update(series)
    _COVERED[article] = (fstart.strftime("%Y%m%d"), end.strftime("%Y%m%d"))


def team_window_views(team_name: str, date: str, days: int = ATTENTION_DAYS) -> int | None:
    """Pageviews for a team in the `days` ending the day BEFORE `date`."""
    article = _article(team_name)
    end = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    _ensure_series(article, start, end)
    series = _SERIES.get(article)
    if not series:
        return None
    total, hit = 0, False
    d = start
    while d <= end:
        v = series.get(d.strftime("%Y%m%d"))
        if v is not None:
            total += v
            hit = True
        d += dt.timedelta(days=1)
    return total if hit else None


def team_attention_counts(teams: list[tuple[str, str]], date: str,
                          days: int = ATTENTION_DAYS) -> dict[str, int]:
    """{team_name: trailing-window pageviews} for the slate's teams."""
    out: dict[str, int] = {}
    for name, _abbr in teams:
        v = team_window_views(name, date, days)
        if v is not None:
            out[name] = v
    log.info("wiki: pageviews for %d/%d team(s)", len(out), len(teams))
    return out
