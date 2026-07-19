"""Ingestion from stats.nba.com via the `nba_api` package.

Primary source for detailed box scores and advanced stats.

TODO: implement fetch_games(season), fetch_team_box_score(game_id),
fetch_player_box_score(game_id). Respect stats.nba.com rate limits
(add delay between calls; this endpoint blocks aggressive polling).
"""
