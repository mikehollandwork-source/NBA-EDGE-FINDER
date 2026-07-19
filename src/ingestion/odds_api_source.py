"""Live odds polling via The Odds API (free tier, ~500 requests/month).

Used going forward (not for backtesting): polled on a schedule to build
our own line-movement history (open -> live -> close) since the free
tier does not retain historical odds itself.

Requires ODDS_API_KEY in a local .env (not committed).

TODO: implement fetch_current_odds(sport='basketball_nba') and a
snapshot writer that tags each poll as 'opening' (first snapshot seen
for a game) or 'live'/'closing' based on timing.
"""
