"""Fetches every public-sentiment source ONCE per run (matching the MLB
repo's main.py pattern -- consensus/forum/etc. are fetched once and
shared across every game, not re-fetched per game) and writes one
public_sentiment_snapshots row per source per game.

This module only COLLECTS and STORES each source's raw read -- it does
not compute a combined "majority side" verdict or otherwise interpret
the numbers. That's deliberately left out: the pick logic built on top of
these signals is being designed separately, not baked in here.

NOT YET TESTED against live data -- every underlying source is blocked by
this environment's egress policy. Team matching between sources is
best-effort (see nba_teams.py) and will need adjustment once real
responses are in hand.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.db.connection import get_connection

from . import covers_source, public_odds_sources, reddit_source, wiki_source
from .nba_teams import ABBR_TO_FULL, canon_abbr

log = logging.getLogger("public_sentiment_collector")


def collect(games: list[dict], date: str, db_path=None) -> dict:
    """games: [{game_id, home_abbr, away_abbr}, ...] for the slate. Fetches
    every source once, matches rows to games by team abbreviations, and
    writes public_sentiment_snapshots. Each source fails independently
    (soft) so one dead site can't block the others."""
    teams = sorted({(ABBR_TO_FULL.get(g["home_abbr"], g["home_abbr"]), g["home_abbr"])
                    for g in games} |
                   {(ABBR_TO_FULL.get(g["away_abbr"], g["away_abbr"]), g["away_abbr"])
                    for g in games})

    written = 0
    conn = get_connection(db_path) if db_path else get_connection()
    now = datetime.now(timezone.utc).isoformat()

    try:
        consensus = covers_source.consensus()
    except Exception as exc:
        log.warning("covers consensus failed: %s", exc)
        consensus = {}
    try:
        forum_counts = covers_source.forum_majority(teams, date)
    except Exception as exc:
        log.warning("covers forum failed: %s", exc)
        forum_counts = {}
    try:
        extra = public_odds_sources.all_sources()
    except Exception as exc:
        log.warning("scoresandodds/vsin failed: %s", exc)
        extra = {}
    try:
        reddit_counts = reddit_source.reddit_majority(teams, date)
    except Exception as exc:
        log.warning("reddit failed: %s", exc)
        reddit_counts = {}
    try:
        wiki_counts = wiki_source.team_attention_counts(teams, date)
    except Exception as exc:
        log.warning("wiki failed: %s", exc)
        wiki_counts = {}

    for g in games:
        game_id, home, away = g["game_id"], g["home_abbr"], g["away_abbr"]
        home_full, away_full = ABBR_TO_FULL.get(home, home), ABBR_TO_FULL.get(away, away)

        row = consensus.get(f"{away}@{home}".lower()) or consensus.get(f"{away_full}@{home_full}".lower())
        if row:
            written += _insert(conn, game_id, "covers_consensus", "pct", now,
                               row["home"]["pct"], row["away"]["pct"])

        if home_full in forum_counts or away_full in forum_counts:
            written += _insert(conn, game_id, "covers_forum", "mention_count", now,
                               forum_counts.get(home_full, 0), forum_counts.get(away_full, 0))

        for src_name, rows in extra.items():
            match = next((r for r in rows if canon_abbr(r["away_abbr"]) == away
                         and canon_abbr(r["home_abbr"]) == home), None)
            if match:
                written += _insert(conn, game_id, src_name, "pct", now,
                                   match["home_pct"], match["away_pct"])

        if home_full in reddit_counts or away_full in reddit_counts:
            written += _insert(conn, game_id, "reddit", "mention_count", now,
                               reddit_counts.get(home_full, 0), reddit_counts.get(away_full, 0))

        if home_full in wiki_counts or away_full in wiki_counts:
            written += _insert(conn, game_id, "wiki", "pageviews", now,
                               wiki_counts.get(home_full, 0), wiki_counts.get(away_full, 0))

    conn.commit()
    conn.close()
    return {"snapshots_written": written}


def _insert(conn, game_id: str, source: str, value_type: str, snapshot_time: str,
           home_value, away_value) -> int:
    conn.execute(
        """INSERT INTO public_sentiment_snapshots
           (game_id, source, snapshot_time, value_type, home_value, away_value)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (game_id, source, snapshot_time, value_type, home_value, away_value),
    )
    return 1
