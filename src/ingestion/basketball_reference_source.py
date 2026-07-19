"""Ingestion from Basketball-Reference via scraping (no official API).

Used for historical depth and advanced metrics, and as a third
cross-check source. No official rate limit is published; be
conservative (throttle requests, cache pages) to avoid getting blocked.

TODO: implement fetch_team_game_log(team, season), fetch_player_game_log(...).
"""
