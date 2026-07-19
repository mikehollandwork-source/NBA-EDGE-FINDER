"""Extra public-betting-% sources, to cross-check covers' consensus -- ported
from the MLB repo's public_sources.py (Scores & Odds + VSIN sections; the
Polymarket helper there is superseded by our own polymarket_source.py).

Scores & Odds and VSIN both publish TWO independent splits per game:
  % of Bets  -> share of tickets (the public)
  % of Money -> share of dollars (sharper -- where the money actually is)
The bets-vs-money divergence is the real "public % doesn't match the money"
tell -- this is what our own opening-favorite RLM approximation was
standing in for. Each is exposed as its own source name
('scoresandodds_bets'/'scoresandodds_money', 'vsin_bets'/'vsin_money').

UNVERIFIED selectors (same caveat as the MLB repo): pinned from that
project's captured pages, not tested against NBA's actual markup. Every
parser fails soft -- on any problem it logs a fingerprint and returns [].
Set PUBLIC_DEBUG=1 for one run to dump raw HTML into data/public_debug/.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .nba_teams import VALID_ABBRS, canon_abbr, name_to_abbr

log = logging.getLogger("public_odds_sources")

SCORESODDS_URL = "https://www.scoresandodds.com/nba/consensus-picks"
VSIN_URL = "https://data.vsin.com/nba/betting-splits/"

TIMEOUT = 20
POLITE_DELAY = 1.0
SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": "Mozilla/5.0 (compatible; nba-edge-finder/1.0; personal research)"}
)

DEBUG = os.environ.get("PUBLIC_DEBUG") == "1"
DEBUG_DIR = Path("data/public_debug")


def _fetch(url: str) -> tuple[BeautifulSoup | None, str, str]:
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        time.sleep(POLITE_DELAY)
        if DEBUG:
            _dump(url, resp.text)
        return BeautifulSoup(resp.text, "html.parser"), resp.text, str(resp.url)
    except Exception as exc:
        log.warning("public source fetch failed for %s: %s", url, exc)
        return None, "", url


def _dump(url: str, text: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"\W+", "_", url.split("//", 1)[-1])[:80] + ".html"
        (DEBUG_DIR / name).write_text(text, encoding="utf-8")
    except Exception as exc:
        log.warning("public-source debug dump failed: %s", exc)


def _fingerprint(text: str, final_url: str, soup: BeautifulSoup, what: str) -> None:
    title = soup.title.get_text(strip=True) if soup.title else ""
    classes = sorted({c for el in soup.find_all(class_=True)
                      for c in el.get("class", [])})[:25]
    log.warning("%s parse empty | final_url=%s | title=%r | bytes=%d | tables=%d | "
                "sample_classes=%s", what, final_url, title, len(text),
                len(soup.find_all("table")), classes)


def _parse_scoresodds(soup: BeautifulSoup) -> list[dict]:
    """Each game has consensus blocks for Moneyline/Spread/Total; keep only
    the one whose two labels are bare team abbreviations (Moneyline)."""
    out: list[dict] = []
    seen: set = set()
    for li in soup.select("li.consensus"):
        toks = [t.strip() for t in li.stripped_strings]
        if "% of Bets" not in toks:
            continue
        bi = toks.index("% of Bets")
        if bi == 0 or bi + 1 >= len(toks):
            continue
        away, home = canon_abbr(toks[bi - 1]), canon_abbr(toks[bi + 1])
        if away not in VALID_ABBRS or home not in VALID_ABBRS:
            continue
        pcts = [int(p[:-1]) for p in toks[bi + 2:] if re.fullmatch(r"\d{1,3}%", p)]
        if len(pcts) < 4:
            continue
        key = (away, home)
        if key in seen:
            continue
        seen.add(key)
        out.append({"away_abbr": away, "home_abbr": home,
                    "away_bets": pcts[0], "home_bets": pcts[1],
                    "away_money": pcts[2], "home_money": pcts[3]})
    return out


def scoresandodds_consensus() -> list[dict]:
    """Scores & Odds NBA moneyline consensus: % of Bets + % of Money per game."""
    soup, text, final_url = _fetch(SCORESODDS_URL)
    if soup is None:
        return []
    out = _parse_scoresodds(soup)
    if not out:
        _fingerprint(text, final_url, soup, "scoresodds")
    return out


_VSIN_ML = re.compile(r"^[+-]\d{3,4}$")
_VSIN_PCT = re.compile(r"^(\d{1,3})%$")


def _parse_vsin(soup: BeautifulSoup) -> list[dict]:
    """VSiN betting-splits page: each team's text run carries a signed ML
    price token followed by its handle% (money) then bets% (tickets)."""
    toks = [t for t in soup.stripped_strings]
    entries: list[tuple[str, int, int]] = []
    i = 0
    while i < len(toks):
        ab = name_to_abbr(toks[i])
        if not ab:
            i += 1
            continue
        j, handle, bets = i + 1, None, None
        while j < len(toks) and j - i < 20 and name_to_abbr(toks[j]) is None:
            if _VSIN_ML.fullmatch(toks[j]):
                pcts = []
                k = j + 1
                while k < len(toks) and len(pcts) < 2 and k - j < 6:
                    m = _VSIN_PCT.fullmatch(toks[k])
                    if m:
                        pcts.append(int(m.group(1)))
                    elif name_to_abbr(toks[k]):
                        break
                    k += 1
                if len(pcts) == 2:
                    handle, bets = pcts
                break
            j += 1
        if handle is not None:
            entries.append((ab, handle, bets))
        i = max(j, i + 1)
    out, seen = [], set()
    for a, h in zip(entries[0::2], entries[1::2]):
        if (a[0], h[0]) in seen:
            continue
        seen.add((a[0], h[0]))
        out.append({"away_abbr": a[0], "home_abbr": h[0],
                    "away_money": a[1], "home_money": h[1],
                    "away_bets": a[2], "home_bets": h[2]})
    return out


def vsin_splits() -> list[dict]:
    """VSiN's DraftKings NBA betting splits: moneyline handle% (money) +
    bets% (tickets) per game."""
    soup, text, final_url = _fetch(VSIN_URL)
    if soup is None:
        return []
    out = _parse_vsin(soup)
    if not out:
        _fingerprint(text, final_url, soup, "vsin")
    return out


def all_sources() -> dict[str, list[dict]]:
    """Every extra public source, fetched independently so one dead site
    can't sink the others. '*_bets' = ticket share (public); '*_money' =
    dollar share (sharp side, never faded on its own)."""
    out: dict[str, list[dict]] = {}
    try:
        so = scoresandodds_consensus()
        out["scoresodds_bets"] = _split(so, "bets")
        out["scoresodds_money"] = _split(so, "money")
    except Exception as exc:
        log.warning("scoresandodds failed: %s", exc)
    try:
        vs = vsin_splits()
        out["vsin_bets"] = _split(vs, "bets")
        out["vsin_money"] = _split(vs, "money")
    except Exception as exc:
        log.warning("vsin failed: %s", exc)
    return out


def _split(rows: list[dict], kind: str) -> list[dict]:
    return [{"away_abbr": r["away_abbr"], "home_abbr": r["home_abbr"],
             "away_pct": r[f"away_{kind}"], "home_pct": r[f"home_{kind}"]}
            for r in rows if f"away_{kind}" in r]
