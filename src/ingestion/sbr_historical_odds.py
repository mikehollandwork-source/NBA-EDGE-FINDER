"""Ingestion of SportsbookReviewsOnline historical odds files.

Used for backtesting: these season-by-season files already contain
opening and closing lines for past games, which no free live API
provides retroactively.

TODO: implement load_season_odds(season) to parse the downloaded
file(s) into odds_snapshots rows (line_type='opening'/'closing').
"""
