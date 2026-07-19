"""Live market-implied probability from Polymarket, used as an
independent real-money-weighted check on sportsbook lines and as the
"live money once games start" signal.

Polymarket is a prediction market, not a sportsbook -- there's no fixed
open/close, just continuous trading. We treat the first snapshot seen
for a market as 'opening', everything before tip-off as 'live', and
keep polling into the game itself (Polymarket markets for a game
typically don't resolve until the final result, so price continues to
move in-game) -- this is what "if possible" in the ask refers to: not
every NBA game will have a Polymarket market, since Polymarket doesn't
guarantee full slate coverage the way a sportsbook does.

NOT YET TESTED against live data -- both gamma-api.polymarket.com and
clob.polymarket.com are currently blocked by this environment's egress
policy. Endpoint shapes below match Polymarket's public docs as of
this writing; verify once network access is available, especially:
  - how NBA markets are tagged/searchable via the Gamma API (exact tag
    slug may differ from 'nba')
  - whether outcomePrices ordering reliably matches the outcomes list
    order (assumed here, should be double-checked against a real
    response)
"""

from datetime import datetime, timezone
import json

import requests

from src.db.connection import get_connection

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
SOURCE_NAME = "polymarket"


def find_market_for_game(home_team: str, away_team: str, game_date: str):
    """Best-effort search for a Polymarket market matching this game.

    Not guaranteed to find one -- Polymarket doesn't list every NBA
    game. Returns None if nothing matches closely enough.
    """
    resp = requests.get(
        f"{GAMMA_BASE_URL}/markets",
        params={"tag": "nba", "closed": "false", "limit": 200},
        timeout=15,
    )
    resp.raise_for_status()
    markets = resp.json()

    for market in markets:
        question = market.get("question", "").lower()
        if home_team.lower() in question and away_team.lower() in question:
            return market
    return None


def get_current_prices(market: dict):
    """Return {outcome_name: implied_probability} for a market, read
    live from the CLOB order book (midpoint) rather than the Gamma
    API's possibly-stale outcomePrices field.
    """
    outcomes = json.loads(market["outcomes"])
    token_ids = json.loads(market["clobTokenIds"])

    prices = {}
    for outcome, token_id in zip(outcomes, token_ids):
        resp = requests.get(
            f"{CLOB_BASE_URL}/midpoint", params={"token_id": token_id}, timeout=15
        )
        resp.raise_for_status()
        prices[outcome] = float(resp.json()["mid"])
    return prices


def snapshot_game(game_id: str, home_team: str, away_team: str, game_date: str, db_path=None):
    """Find and snapshot the Polymarket price for one game, if a market
    exists for it. Safe to call repeatedly (e.g. every few minutes once
    a game starts) to build the live line-movement history.
    """
    conn = get_connection(db_path) if db_path else get_connection()

    market = find_market_for_game(home_team, away_team, game_date)
    if market is None:
        conn.close()
        return {"found": False}

    prices = get_current_prices(market)
    home_prob = prices.get(home_team)
    away_prob = prices.get(away_team)

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
         line_type, home_prob, away_prob),
    )
    conn.commit()
    conn.close()
    return {"found": True, "home_prob": home_prob, "away_prob": away_prob}
