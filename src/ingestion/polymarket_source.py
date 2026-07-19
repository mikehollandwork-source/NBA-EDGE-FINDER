"""Live market-implied probability from Polymarket, used as an independent
real-money-weighted check on sportsbook lines and as the "live money once
games start" signal.

Rewritten to match the MLB repo's proven, actually-exercised pattern
(pm_books.py / public_sources.py) instead of the original guess in this
file, which used unverified endpoints:
  - market discovery: Gamma API's /events?tag_slug=nba (not /markets?tag=nba)
  - live price: CLOB /book best bid/ask (not /midpoint), matching how
    Kalshi's top_of_book works so both venues read the same way
  - a hard lesson from that project's production use: "gamma's name->token
    pairing can't be trusted blind" -- a market's two outcome tokens are
    validated against a reference probability (e.g. from ESPN/covers)
    before being trusted, rather than assumed correct from the outcome
    label alone.

Polymarket is a prediction market, not a sportsbook -- there's no fixed
open/close, just continuous trading. The first snapshot seen for a game
is tagged 'opening', everything before tip-off 'live', and polling
continues into the game itself since Polymarket markets don't resolve
until the final result -- this is what "if possible" refers to: not
every NBA game will have a Polymarket market.

NOT YET TESTED against live data -- gamma-api.polymarket.com and
clob.polymarket.com are blocked by this environment's egress policy.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import requests

from src.db.connection import get_connection

from .nba_teams import name_to_abbr

log = logging.getLogger("polymarket_source")

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"
SOURCE_NAME = "polymarket"
TIMEOUT = 15
SIDE_TOL = 0.12  # a pre-game price must sit within this of the reference to be trusted

_INDEX_CACHE: dict | None = None


def _get(url: str, **params):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "nba-edge-finder (personal research)"})
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("polymarket fetch failed (%s): %s", url, exc)
        return None


def open_market_index(force_refresh: bool = False) -> dict:
    """{(away_abbr, home_abbr): {abbr: token_id}} for OPEN NBA game markets.
    Cached per pipeline run; pass force_refresh=True to re-pull."""
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and not force_refresh:
        return _INDEX_CACHE

    index: dict = {}
    offset = 0
    while True:
        batch = _get(GAMMA_EVENTS, tag_slug="nba", closed="false", limit=100, offset=offset)
        if not isinstance(batch, list) or not batch:
            break
        for ev in batch:
            for m in ev.get("markets") or []:
                try:
                    outcomes, tokens = m.get("outcomes"), m.get("clobTokenIds")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(tokens, str):
                        tokens = json.loads(tokens)
                    if not outcomes or not tokens or len(outcomes) != 2:
                        continue
                    a1 = name_to_abbr(str(outcomes[0]))
                    a2 = name_to_abbr(str(outcomes[1]))
                    if not a1 or not a2 or a1 == a2:
                        continue
                    by_abbr = {a1: tokens[0], a2: tokens[1]}
                    index[(a1, a2)] = by_abbr
                    index[(a2, a1)] = by_abbr
                except Exception:
                    continue
        offset += 100
        if len(batch) < 100:
            break
    log.info("polymarket: %d open NBA game market pair(s)", len(index) // 2)
    _INDEX_CACHE = index
    return index


def _book_price(token_id: str) -> float | None:
    """Best-bid estimate of that token's win probability (0-1), from the
    CLOB order book -- same top-of-book approach as kalshi_source, so
    the two venues are read consistently. None on empty book/failure."""
    data = _get(CLOB_BOOK, token_id=token_id)
    bids = (data or {}).get("bids") or []
    if not bids:
        return None
    try:
        return max(float(b["price"]) for b in bids)
    except (KeyError, ValueError, TypeError):
        return None


def find_market_for_game(home_abbr: str, away_abbr: str) -> dict | None:
    index = open_market_index()
    tokens = index.get((away_abbr, home_abbr))
    if not tokens:
        return None
    return tokens


def get_current_prices(tokens: dict[str, str], reference_home_prob: float | None,
                       home_abbr: str, away_abbr: str) -> dict[str, float] | None:
    """{home_abbr: prob, away_abbr: prob} from the order book. When a
    reference probability is supplied (e.g. from ESPN/covers), the home
    side's price must land within SIDE_TOL of it -- otherwise the
    outcome-label-to-token pairing is treated as unreliable for this
    market and the read is dropped rather than trusted blind."""
    home_price = _book_price(tokens.get(home_abbr))
    away_price = _book_price(tokens.get(away_abbr))
    if home_price is None or away_price is None:
        return None

    if reference_home_prob is not None and abs(home_price - reference_home_prob) > SIDE_TOL:
        log.warning("polymarket: %s vs %s price %.2f is >%.2f from reference %.2f -- "
                    "dropping (likely name/token mismatch)",
                    home_abbr, away_abbr, home_price, SIDE_TOL, reference_home_prob)
        return None

    return {home_abbr: home_price, away_abbr: away_price}


def snapshot_game(game_id: str, home_abbr: str, away_abbr: str, game_date: str,
                  reference_home_prob: float | None = None, db_path=None):
    """Find and snapshot the Polymarket price for one game, if a market
    exists for it. Safe to call repeatedly to build the live line-
    movement history. reference_home_prob (from ESPN/covers, de-vigged)
    is used to validate the token pairing -- pass it when available."""
    tokens = find_market_for_game(home_abbr, away_abbr)
    if tokens is None:
        return {"found": False}

    prices = get_current_prices(tokens, reference_home_prob, home_abbr, away_abbr)
    if prices is None:
        return {"found": False, "reason": "no book price or reference mismatch"}

    conn = get_connection(db_path) if db_path else get_connection()
    already_seen = conn.execute(
        "SELECT 1 FROM odds_snapshots WHERE game_id = ? AND source = ? LIMIT 1",
        (game_id, SOURCE_NAME),
    ).fetchone()
    line_type = "opening" if not already_seen else "live"

    conn.execute(
        """INSERT INTO odds_snapshots
           (game_id, source, book, snapshot_time, line_type,
            home_implied_prob, away_implied_prob)
           VALUES (?, ?, 'polymarket', ?, ?, ?, ?)""",
        (game_id, SOURCE_NAME, datetime.now(timezone.utc).isoformat(),
         line_type, prices[home_abbr], prices[away_abbr]),
    )
    conn.commit()
    conn.close()
    return {"found": True, "home_prob": prices[home_abbr], "away_prob": prices[away_abbr]}
