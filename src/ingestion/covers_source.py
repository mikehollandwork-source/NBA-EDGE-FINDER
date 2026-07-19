"""covers.com scraping: public betting consensus % + forum-post sentiment,
ported from the MLB repo's covers.py (same site, same parsing technique --
the actual consensus numbers live in covers' embedded Next.js
__NEXT_DATA__ JSON, with a table-heuristic fallback).

UNVERIFIED URLs: the MLB repo's forum URL includes a numeric category id
specific to MLB (`/forum/mlb-betting-27`) that had to be found by visiting
the site -- the NBA equivalent below is a best-effort guess following the
same `/forum/<sport>-betting[-<id>]` pattern and WILL likely need
correcting after the first live run (set COVERS_DEBUG=1 to dump the raw
HTML into data/covers_debug/, same as the MLB repo's approach). The
consensus and odds URLs follow covers' documented sport/league path
convention and are lower-risk.

covers.com has Terms of Service; this is intended for personal, low-volume
research. Requests are rate-limited and identify a custom User-Agent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("covers_source")

CONSENSUS_URL = "https://contests.covers.com/consensus/topconsensus/nba/overall"
FORUM_URL = "https://www.covers.com/forum/nba-betting"  # UNVERIFIED -- see module docstring
ODDS_URL = "https://www.covers.com/sport/basketball/nba/odds"

TIMEOUT = 20
POLITE_DELAY = 1.0
SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": "Mozilla/5.0 (compatible; nba-edge-finder/1.0; personal research)"}
)

DEBUG = os.environ.get("COVERS_DEBUG") == "1"
DEBUG_DIR = Path("data/covers_debug")


def _fetch(url: str) -> tuple[BeautifulSoup | None, str, str]:
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        time.sleep(POLITE_DELAY)
        if DEBUG:
            _dump(url, resp.text)
        return BeautifulSoup(resp.text, "html.parser"), resp.text, str(resp.url)
    except Exception as exc:
        log.warning("covers fetch failed for %s: %s", url, exc)
        return None, "", url


def _dump(url: str, text: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"\W+", "_", url.split("//", 1)[-1])[:80] + ".html"
        (DEBUG_DIR / name).write_text(text, encoding="utf-8")
    except Exception as exc:
        log.warning("covers debug dump failed: %s", exc)


def _next_data(soup: BeautifulSoup) -> dict | None:
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except Exception:
            pass
    return None


def _fingerprint(text: str, final_url: str, soup: BeautifulSoup, what: str) -> None:
    title = soup.title.get_text(strip=True) if soup.title else ""
    classes = sorted({c for el in soup.find_all(class_=True)
                      for c in el.get("class", [])})[:25]
    log.warning(
        "%s parse empty | final_url=%s | title=%r | bytes=%d | __NEXT_DATA__=%s | "
        "tables=%d | sample_classes=%s",
        what, final_url, title, len(text), _next_data(soup) is not None,
        len(soup.find_all("table")), classes,
    )


def _pct(text: str) -> float | None:
    m = re.search(r"(\d{1,3})\s*%", text)
    return float(m.group(1)) if m else None


def consensus() -> dict[str, dict]:
    """{"away@home": {"away": {abbr, pct, moneyline}, "home": {...}}}. {} on failure."""
    soup, text, final_url = _fetch(CONSENSUS_URL)
    if soup is None:
        return {}
    out = _parse_consensus_rows(soup)
    if not out:
        _fingerprint(text, final_url, soup, "consensus")
    return out


def _parse_consensus_rows(soup: BeautifulSoup) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tr in soup.select("tr"):
        teams = [a.get_text(strip=True) for a in tr.find_all("a")
                 if "pickleadersbyteam" in a.get("href", "")]
        spans = tr.select(".covers-CoversConsensus-consensusTable--high,"
                          " .covers-CoversConsensus-consensusTable--low")
        if len(teams) < 2 or len(spans) < 2:
            continue
        away_pct, home_pct = _pct(spans[0].get_text()), _pct(spans[1].get_text())
        if away_pct is None or home_pct is None:
            continue
        odds = re.findall(r"(?<![\d.])[+-]\d{3,4}(?![\d.])", tr.get_text(" "))
        away_ml = odds[0] if len(odds) >= 2 else None
        home_ml = odds[1] if len(odds) >= 2 else None
        key = f"{teams[0]}@{teams[1]}".lower()
        out[key] = {
            "away": {"abbr": teams[0], "pct": away_pct, "moneyline": away_ml},
            "home": {"abbr": teams[1], "pct": home_pct, "moneyline": home_ml},
        }
    return out


def slate_lines() -> list[dict]:
    """Open->current moneyline for every game on covers' NBA odds page (gap-fill
    for whatever ESPN doesn't have). {away_abbr, home_abbr, away_open,
    away_current, home_open, home_current}."""
    soup, _, _ = _fetch(ODDS_URL)
    if soup is None:
        return []
    out: list[dict] = []
    seen: set = set()
    for r in soup.select(".oddsGameRow"):
        abbr = []
        for a in r.select('a[href*="/nba/matchup/"]'):
            m = re.match(r"^([A-Z]{2,3})\b", a.get_text(" ", strip=True))
            if m and m.group(1) not in abbr:
                abbr.append(m.group(1))
        if len(abbr) < 2 or (abbr[0], abbr[1]) in seen:
            continue
        op = r.select_one(".opening-lines-div")
        opens = re.findall(r"(?<![\d.])[+-]\d{3,4}(?![\d.])", op.get_text(" ") if op else "")
        ao, ho = (int(opens[0]), int(opens[1])) if len(opens) >= 2 else (None, None)

        def _cur(cls: str) -> int | None:
            el = r.select_one("." + cls)
            m = re.search(r"(?<![\d.])[+-]\d{3,4}(?![\d.])", el.get_text(" ")) if el else None
            return int(m.group(0)) if m else None

        ca = _cur("covers-CoversMatchups-topOddsAway")
        ch = _cur("covers-CoversMatchups-topOddsHome")
        if (ca is None or ch is None) and (ao is None or ho is None):
            continue
        seen.add((abbr[0], abbr[1]))
        out.append({"away_abbr": abbr[0], "home_abbr": abbr[1],
                    "away_open": ao, "home_open": ho,
                    "away_current": ca if ca is not None else ao,
                    "home_current": ch if ch is not None else ho})
    log.info("covers slate: parsed %d game line(s)", len(out))
    return out


# Run-line/spread language, same reasoning as the MLB module: a pick
# written next to one of these is a SPREAD pick, not moneyline.
SPREAD_RE = re.compile(r"[+-]\s?\d{1,2}\.5|\bspread\b|\bats\b|\bcover(?:s|ed|ing)?\b", re.I)
ML_RE = re.compile(r"\bml\b|\bmoney\s?line\b|\bm/?l\b|\bstraight[- ]?up\b|\bsu\b", re.I)


def _team_patterns(name: str, abbr: str = "") -> dict:
    parts = name.strip().lower().split()
    subs = {name.strip().lower()}
    if parts:
        subs.add(parts[-1])
        subs.add(" ".join(parts[:-1]))
    words = {abbr.lower()} if abbr else set()
    return {"subs": [s for s in subs if s], "words": [w for w in words if w]}


def _mention_spans(low_text: str, pats: dict) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for s in pats["subs"]:
        i = low_text.find(s)
        while i != -1:
            spans.append((i, i + len(s)))
            i = low_text.find(s, i + 1)
    for w in pats["words"]:
        spans.extend(m.span() for m in re.finditer(rf"\b{re.escape(w)}\b", low_text))
    return spans


def _post_moneyline_teams(low_text: str, matchers: dict) -> set[str]:
    marks: list[tuple[int, int, str]] = []
    for name, pats in matchers.items():
        for a, b in _mention_spans(low_text, pats):
            marks.append((a, b, name))
    marks.sort()
    out: set[str] = set()
    for i, (a, _b, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(low_text)
        scope = low_text[a:end]
        if ML_RE.search(scope) or not SPREAD_RE.search(scope):
            out.add(name)
    return out


def _abs_forum(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://www.covers.com" + href if href.startswith("/") else href


FORUM_MAX_THREADS = 40


def _thread_links(soup: BeautifulSoup) -> list[str]:
    seen: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"/forum/nba-betting.*-\d{6,}/?$", href):
            url = _abs_forum(href)
            if url not in seen:
                seen.append(url)
    return seen


def _todays_thread_posts(listing_soup: BeautifulSoup, date: str,
                         max_threads: int = FORUM_MAX_THREADS) -> list[str]:
    posts: list[str] = []
    for url in _thread_links(listing_soup)[:max_threads]:
        tsoup, _, _ = _fetch(url)
        if tsoup is None:
            continue
        pages = [tsoup]
        try:
            extra = []
            for a in tsoup.select(".covers-CoversForum-paginationContainer a[href]"):
                u = _abs_forum(a["href"])
                if u != url and u not in extra:
                    extra.append(u)
            for u in extra[-3:]:
                psoup, _, _ = _fetch(u)
                if psoup is not None:
                    pages.append(psoup)
        except Exception as exc:
            log.warning("thread pagination failed: %s", exc)
        for page in pages:
            for brick in page.select(".covers-CoversForum-postBrick"):
                stamp = brick.find("time", attrs={"datetime": True})
                if not stamp or stamp.get("datetime", "")[:10] != date:
                    continue
                body_el = brick.select_one(".raw-post-body")
                body = body_el.get_text(" ", strip=True) if body_el else ""
                if body:
                    posts.append(body)
    return posts


def forum_majority(teams: list[tuple[str, str]], date: str) -> dict[str, int]:
    """Tally how often each team is mentioned as a moneyline pick across
    that day's NBA forum posts. `teams` = [(full_name, abbr), ...]."""
    soup, text, final_url = _fetch(FORUM_URL)
    counts = {name: 0 for name, _ in teams}
    if soup is None:
        return counts
    posts = _todays_thread_posts(soup, date)
    if not posts:
        _fingerprint(text, final_url, soup, "forum")
    matchers = {name: _team_patterns(name, abbr) for name, abbr in teams}
    for body in posts:
        for name in _post_moneyline_teams(body.lower(), matchers):
            counts[name] += 1
    return counts
