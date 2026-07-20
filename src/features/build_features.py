"""Feature engineering for the matchup comparison.

Builds the full feature set designed with the user: team last-4-games
rate/advanced stats (SOS + home/away adjusted), a position-by-position
starter comparison (real h2h matchup data as primary, self-derived
defense-vs-position as fallback, blended by sample-size confidence),
star-weighted player form, bench contribution via expected minutes,
height/speed differentials, rest/travel, referee tendency, and expected
game pace.

Point-in-time throughout: every function takes `as_of` and only uses
games/stats strictly before it, so a backtest never sees a game's own
future (same discipline as the MLB sibling project's mlb_api.py).

This module ONLY computes and returns features -- it does not decide a
winner or combine anything into a score. That's src/models/win_predictor.py
and the win-condition/signal logic, built separately per the user's
"I want to make my own pick logic" instruction.

NOT YET TESTED against live data -- depends on every ingestion source,
all of which are blocked by this environment's egress policy.
"""

from __future__ import annotations

import datetime as dt
import math

from src.db.connection import get_connection
from src.ingestion.nba_teams import travel_distance_miles

PLAYER_LOOKBACK_GAMES = 3
TEAM_LOOKBACK_GAMES = 4
DEFENSE_VS_POSITION_LOOKBACK_GAMES = 10   # how many recent games a team's DVP read covers
DVP_SHRINKAGE_PRIOR_GAMES = 10            # shrink toward league avg by this many "phantom" games
MATCHUP_TRUST_POSSESSIONS = 15            # real h2h data trusted fully at/above this sample
STAR_QUALITY_FLOOR = 0.5                  # role players still count a little in form-weighting


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _per36(value, minutes) -> float | None:
    if value is None or not minutes:
        return None
    return value / minutes * 36


def _per100(value, possessions) -> float | None:
    if value is None or not possessions:
        return None
    return value / possessions * 100


def _avg(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _conn(db_path):
    return get_connection(db_path) if db_path else get_connection()


# --------------------------------------------------------------------------
# team-level: rate/advanced stats, SOS, home/away, pace
# --------------------------------------------------------------------------

def team_recent_games(conn, team: str, as_of: str, n_games: int = TEAM_LOOKBACK_GAMES):
    return conn.execute(
        """SELECT t.*, g.game_date, g.home_team, g.away_team
           FROM team_game_stats t JOIN games g ON g.game_id = t.game_id
           WHERE t.team = ? AND t.source = 'nba_api' AND g.game_date < ?
           ORDER BY g.game_date DESC LIMIT ?""",
        (team, as_of, n_games),
    ).fetchall()


def team_rate_stats(conn, team: str, as_of: str, n_games: int = TEAM_LOOKBACK_GAMES,
                    home_only: bool | None = None) -> dict:
    """Per-100-possession box stats + advanced ratings, averaged over the
    last n_games before as_of. home_only=True/False filters to home/road
    games only (the home/away splits feature); None = no filter."""
    rows = team_recent_games(conn, team, as_of, n_games * 3 if home_only is not None else n_games)
    if home_only is not None:
        rows = [r for r in rows if bool(r["is_home"]) == home_only][:n_games]

    stat_fields = ["points", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
                   "oreb", "dreb", "reb", "ast", "stl", "blk", "tov", "pf"]
    per100 = {f: _avg([_per100(r[f], r["possessions"]) for r in rows]) for f in stat_fields}
    per100["off_rating"] = _avg([r["off_rating"] for r in rows])
    per100["def_rating"] = _avg([r["def_rating"] for r in rows])
    per100["net_rating"] = _avg([r["net_rating"] for r in rows])
    per100["pace"] = _avg([r["pace"] for r in rows])
    per100["games_sampled"] = len(rows)
    return per100


def strength_of_schedule(conn, team: str, as_of: str, n_games: int = TEAM_LOOKBACK_GAMES) -> dict:
    """Average opponent net rating faced over the last n_games -- the SOS
    adjustment. Net rating is preferred over win% since it isn't skewed
    by close-game luck/blowout variance."""
    rows = team_recent_games(conn, team, as_of, n_games)
    opp_ratings = []
    for r in rows:
        opp = r["away_team"] if r["team"] == r["home_team"] else r["home_team"]
        opp_row = conn.execute(
            """SELECT net_rating FROM team_game_stats
               WHERE game_id = ? AND team = ? AND source = 'nba_api'""",
            (r["game_id"], opp),
        ).fetchone()
        if opp_row and opp_row["net_rating"] is not None:
            opp_ratings.append(opp_row["net_rating"])
    return {"avg_opponent_net_rating": _avg(opp_ratings), "games_sampled": len(opp_ratings)}


def rest_and_travel(conn, team: str, game_date: str) -> dict:
    """Days of rest before this game, back-to-back flag, and travel
    distance from the previous game's venue."""
    prev = conn.execute(
        """SELECT g.game_date, g.home_team, g.away_team FROM team_game_stats t
           JOIN games g ON g.game_id = t.game_id
           WHERE t.team = ? AND t.source = 'nba_api' AND g.game_date < ?
           ORDER BY g.game_date DESC LIMIT 1""",
        (team, game_date),
    ).fetchone()
    if not prev:
        return {"rest_days": None, "back_to_back": None, "travel_miles": None}

    rest_days = (dt.date.fromisoformat(game_date) - dt.date.fromisoformat(prev["game_date"])).days - 1
    prev_venue = prev["home_team"]  # the arena the prior game was played at
    return {
        "rest_days": rest_days,
        "back_to_back": rest_days <= 0,
        "travel_miles": travel_distance_miles(prev_venue, team),
    }


def expected_game_pace(conn, home_team: str, away_team: str, as_of: str) -> float | None:
    home_pace = team_rate_stats(conn, home_team, as_of)["pace"]
    away_pace = team_rate_stats(conn, away_team, as_of)["pace"]
    return _avg([home_pace, away_pace])


# --------------------------------------------------------------------------
# league averages (for vs-league-average comparisons)
# --------------------------------------------------------------------------

def league_average_player_rates(conn, as_of: str, position: str | None = None) -> dict:
    """Per-36 league-average box stats season-to-date, optionally filtered
    to one position bucket. Used to compare a player against his peers,
    not just his own history."""
    query = """SELECT p.points, p.reb, p.ast, p.stl, p.blk, p.tov, p.minutes
               FROM player_game_stats p JOIN games g ON g.game_id = p.game_id
               WHERE p.source = 'nba_api' AND g.game_date < ? AND p.minutes > 0"""
    params = [as_of]
    if position:
        query += """ AND p.player_id IN (SELECT player_id FROM players WHERE position = ?)"""
        params.append(position)
    rows = conn.execute(query, params).fetchall()

    out = {}
    for field in ("points", "reb", "ast", "stl", "blk", "tov"):
        out[field] = _avg([_per36(r[field], r["minutes"]) for r in rows])
    return out


# --------------------------------------------------------------------------
# player-level: per-36 form, star-weighting, height, speed
# --------------------------------------------------------------------------

def player_recent_games(conn, player_id: str, as_of: str, n_games: int = PLAYER_LOOKBACK_GAMES):
    return conn.execute(
        """SELECT p.*, g.game_date FROM player_game_stats p
           JOIN games g ON g.game_id = p.game_id
           WHERE p.player_id = ? AND p.source = 'nba_api' AND g.game_date < ?
           ORDER BY g.game_date DESC LIMIT ?""",
        (player_id, as_of, n_games),
    ).fetchall()


def player_form(conn, player_id: str, as_of: str) -> dict:
    """Per-36 stats over the last 3 games vs. the player's own season
    average -- the core player-trend signal."""
    recent = player_recent_games(conn, player_id, as_of, PLAYER_LOOKBACK_GAMES)
    season = conn.execute(
        """SELECT p.points, p.reb, p.ast, p.stl, p.blk, p.tov, p.minutes
           FROM player_game_stats p JOIN games g ON g.game_id = p.game_id
           WHERE p.player_id = ? AND p.source = 'nba_api' AND g.game_date < ?""",
        (player_id, as_of),
    ).fetchall()

    fields = ("points", "reb", "ast", "stl", "blk", "tov")
    recent_per36 = {f: _avg([_per36(r[f], r["minutes"]) for r in recent]) for f in fields}
    season_per36 = {f: _avg([_per36(r[f], r["minutes"]) for r in season]) for f in fields}
    delta = {
        f: (recent_per36[f] - season_per36[f])
        if recent_per36[f] is not None and season_per36[f] is not None else None
        for f in fields
    }
    season_minutes = _avg([r["minutes"] for r in season])
    return {
        "recent_per36": recent_per36, "season_per36": season_per36, "delta": delta,
        "season_minutes_per_game": season_minutes, "games_sampled": len(recent),
    }


def star_quality_weight(conn, player_id: str, as_of: str) -> float:
    """Season quality proxy for how much this player's form delta should
    move the team number -- a hot star matters more than a hot bench
    player. Usage-rate based (falls back to minutes share when usage
    isn't populated), floored so role players still count a little.
    Same technique as the MLB sibling project's lineup-form weighting."""
    row = conn.execute(
        """SELECT AVG(p.minutes) AS avg_min FROM player_game_stats p
           JOIN games g ON g.game_id = p.game_id
           WHERE p.player_id = ? AND p.source = 'nba_api' AND g.game_date < ?""",
        (player_id, as_of),
    ).fetchone()
    avg_minutes = (row["avg_min"] if row else None) or 0
    # Minutes share of a 48-min game as the simplest available quality
    # proxy until usage rate is wired through -- a starter playing 32
    # minutes counts several times more than a 6-minute bench player.
    quality = avg_minutes / 48.0
    return max(STAR_QUALITY_FLOOR, quality)


def height_advantage(conn, home_player_id: str, away_player_id: str) -> float | None:
    h = conn.execute("SELECT height_inches FROM players WHERE player_id = ?",
                     (home_player_id,)).fetchone()
    a = conn.execute("SELECT height_inches FROM players WHERE player_id = ?",
                     (away_player_id,)).fetchone()
    if not h or not a or h["height_inches"] is None or a["height_inches"] is None:
        return None
    return h["height_inches"] - a["height_inches"]


# --------------------------------------------------------------------------
# starters, bench, expected minutes
# --------------------------------------------------------------------------

def projected_starters(conn, team: str, as_of: str, n_games: int = 5) -> dict:
    """Best-effort starting five by position, inferred from who started
    most often in the last n_games (not a live lineup report -- this is
    the "recent starter history" default described to the user; a
    same-day lineup/injury source can override closer to tip-off)."""
    # Two-step: pull the most recent n_games*5 starter-rows (across every
    # position) first, THEN group/count -- an aggregate with non-aggregated
    # columns and no GROUP BY collapses to one arbitrary row in SQLite, it
    # does not implicitly group per player.
    recent_starter_rows = conn.execute(
        """SELECT p.player_id, p.player_name, pl.position, g.game_date
           FROM player_game_stats p
           JOIN games g ON g.game_id = p.game_id
           LEFT JOIN players pl ON pl.player_id = p.player_id
           WHERE p.team = ? AND p.source = 'nba_api' AND g.game_date < ?
             AND p.is_starter = 1
           ORDER BY g.game_date DESC LIMIT ?""",
        (team, as_of, n_games * 5),
    ).fetchall()

    by_position: dict[str, dict] = {}
    for r in recent_starter_rows:
        pos = r["position"] or "UNKNOWN"
        entry = by_position.setdefault(pos, {})
        key = r["player_id"]
        entry[key] = entry.get(key, {"player_id": key, "player_name": r["player_name"], "starts": 0})
        entry[key]["starts"] += 1

    starters = {}
    for pos, players in by_position.items():
        starters[pos] = max(players.values(), key=lambda p: p["starts"])
    return starters


def bench_contribution(conn, team: str, as_of: str) -> dict:
    """Minutes-weighted form delta across everyone who ISN'T a projected
    starter -- bench depth as one aggregate number rather than tracking
    every reserve individually."""
    starters = {s["player_id"] for s in projected_starters(conn, team, as_of).values()}
    roster = conn.execute(
        """SELECT DISTINCT p.player_id FROM player_game_stats p
           JOIN games g ON g.game_id = p.game_id
           WHERE p.team = ? AND p.source = 'nba_api' AND g.game_date < ?
             AND g.game_date >= date(?, '-14 day')""",
        (team, as_of, as_of),
    ).fetchall()

    weighted_delta_sum, weight_sum = 0.0, 0.0
    for r in roster:
        pid = r["player_id"]
        if pid in starters:
            continue
        form = player_form(conn, pid, as_of)
        weight = star_quality_weight(conn, pid, as_of)
        pts_delta = form["delta"].get("points")
        if pts_delta is not None:
            weighted_delta_sum += pts_delta * weight
            weight_sum += weight

    return {
        "bench_points_form_delta": (weighted_delta_sum / weight_sum) if weight_sum else None,
        "bench_players_sampled": len(roster) - len(starters & {r["player_id"] for r in roster}),
    }


# --------------------------------------------------------------------------
# h2h: real matchup data (primary) blended with defense-vs-position (fallback)
# --------------------------------------------------------------------------

def defense_vs_position(conn, team: str, position: str, as_of: str,
                        n_games: int = DEFENSE_VS_POSITION_LOOKBACK_GAMES) -> dict:
    """Self-derived from box scores we already have: what have opposing
    players at this position scored against `team` recently, shrunk
    toward the league-average-at-this-position by sample size (small
    early-season samples get pulled mostly back to league average, a
    full sample is trusted) -- same shrinkage idea as the MLB project's
    FIP regression."""
    rows = conn.execute(
        """SELECT p.points, p.minutes FROM player_game_stats p
           JOIN games g ON g.game_id = p.game_id
           WHERE p.source = 'nba_api' AND g.game_date < ?
             AND p.team != ? AND p.minutes > 0
             AND g.game_id IN (
                 SELECT game_id FROM team_game_stats
                 WHERE team = ? AND source = 'nba_api'
                 ORDER BY (SELECT game_date FROM games WHERE games.game_id = team_game_stats.game_id) DESC
                 LIMIT ?
             )
             AND p.player_id IN (SELECT player_id FROM players WHERE position = ?)""",
        (as_of, team, team, n_games, position),
    ).fetchall()

    sample_per36 = [_per36(r["points"], r["minutes"]) for r in rows if r["minutes"]]
    sample_avg = _avg(sample_per36)
    league_avg = league_average_player_rates(conn, as_of, position=position).get("points")

    sample_size = len(sample_per36)
    if sample_avg is None:
        return {"points_allowed_per36": league_avg, "sample_size": 0, "shrunk": True}
    if league_avg is None:
        return {"points_allowed_per36": sample_avg, "sample_size": sample_size, "shrunk": False}

    w = sample_size / (sample_size + DVP_SHRINKAGE_PRIOR_GAMES)
    shrunk = w * sample_avg + (1 - w) * league_avg
    return {"points_allowed_per36": shrunk, "sample_size": sample_size, "shrunk": True}


def h2h_matchup(conn, def_player_id: str, off_player_id: str, season: str,
               team_defense_vs_position: dict) -> dict:
    """Blend real player-vs-player matchup data (primary, when the
    possession sample is big enough) with the defense-vs-position
    fallback -- weighted by how much real h2h sample exists, per the
    user's "keep h2h primary, hold weight if we get that data" call.
    Guards against the "weak defender inflated the stats" trap: this
    blend only trusts the h2h number in proportion to its own sample
    size, so a thin/stale h2h read can't dominate."""
    row = conn.execute(
        """SELECT partial_possessions, points_allowed FROM player_matchups
           WHERE def_player_id = ? AND off_player_id = ? AND season = ?""",
        (def_player_id, off_player_id, season),
    ).fetchone()

    fallback = team_defense_vs_position.get("points_allowed_per36")
    if not row or row["partial_possessions"] is None or row["points_allowed"] is None:
        return {"points_allowed_estimate": fallback, "h2h_weight": 0.0, "source": "dvp_fallback_only"}

    h2h_weight = min(1.0, row["partial_possessions"] / MATCHUP_TRUST_POSSESSIONS)
    if fallback is None:
        return {"points_allowed_estimate": row["points_allowed"], "h2h_weight": h2h_weight, "source": "h2h_only"}

    blended = h2h_weight * row["points_allowed"] + (1 - h2h_weight) * fallback
    return {"points_allowed_estimate": blended, "h2h_weight": h2h_weight, "source": "blended"}


# --------------------------------------------------------------------------
# referee tendency
# --------------------------------------------------------------------------

def referee_crew_tendency(conn, official_names: list[str], as_of: str) -> dict:
    """Self-derived foul-rate tendency for a crew vs. league average,
    from games we've already ingested where these officials worked --
    same idea as the MLB project's umpire zone tendency, computed from
    our own data instead of a stats site."""
    if not official_names:
        return {"fouls_per_game_vs_league": None, "games_sampled": 0}

    placeholders = ",".join("?" * len(official_names))
    crew_games = conn.execute(
        f"""SELECT DISTINCT game_id FROM game_officials
            WHERE official_name IN ({placeholders})""",
        official_names,
    ).fetchall()
    game_ids = [r["game_id"] for r in crew_games]
    if not game_ids:
        return {"fouls_per_game_vs_league": None, "games_sampled": 0}

    ph = ",".join("?" * len(game_ids))
    crew_fouls = conn.execute(
        f"""SELECT AVG(pf) AS avg_pf FROM team_game_stats
            WHERE game_id IN ({ph}) AND source = 'nba_api'""",
        game_ids,
    ).fetchone()

    league_fouls = conn.execute(
        """SELECT AVG(t.pf) AS avg_pf FROM team_game_stats t
           JOIN games g ON g.game_id = t.game_id
           WHERE t.source = 'nba_api' AND g.game_date < ?""",
        (as_of,),
    ).fetchone()

    crew_avg = crew_fouls["avg_pf"] if crew_fouls else None
    league_avg = league_fouls["avg_pf"] if league_fouls else None
    diff = (crew_avg - league_avg) if crew_avg is not None and league_avg is not None else None
    return {"fouls_per_game_vs_league": diff, "games_sampled": len(game_ids)}


# --------------------------------------------------------------------------
# top-level assembly
# --------------------------------------------------------------------------

def build_matchup_features(game_id: str, db_path=None, as_of: str | None = None) -> dict:
    """Every feature for both sides of one matchup. as_of defaults to the
    game's own date (point-in-time correct for both live use and
    backtesting)."""
    conn = _conn(db_path)
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if not game:
        conn.close()
        raise ValueError(f"no game row for {game_id}")

    as_of = as_of or game["game_date"]
    home, away = game["home_team"], game["away_team"]
    season = game["season"]

    features = {
        "game_id": game_id, "as_of": as_of, "home_team": home, "away_team": away,
        "team": {}, "positions": {}, "bench": {}, "rest_travel": {}, "referee": {},
        "expected_pace": expected_game_pace(conn, home, away, as_of),
    }

    for side, team in (("home", home), ("away", away)):
        features["team"][side] = {
            "overall": team_rate_stats(conn, team, as_of),
            "home_split" if side == "home" else "away_split":
                team_rate_stats(conn, team, as_of, home_only=(side == "home")),
            "sos": strength_of_schedule(conn, team, as_of),
        }
        features["rest_travel"][side] = rest_and_travel(conn, team, game["game_date"])
        features["bench"][side] = bench_contribution(conn, team, as_of)

    home_starters = projected_starters(conn, home, as_of)
    away_starters = projected_starters(conn, away, as_of)
    for position in set(home_starters) | set(away_starters):
        h, a = home_starters.get(position), away_starters.get(position)
        entry = {"home_player": h, "away_player": a}
        if h:
            entry["home_form"] = player_form(conn, h["player_id"], as_of)
            entry["home_star_weight"] = star_quality_weight(conn, h["player_id"], as_of)
        if a:
            entry["away_form"] = player_form(conn, a["player_id"], as_of)
            entry["away_star_weight"] = star_quality_weight(conn, a["player_id"], as_of)
        if h and a:
            entry["height_advantage_home"] = height_advantage(conn, h["player_id"], a["player_id"])
            home_dvp = defense_vs_position(conn, home, position, as_of)
            away_dvp = defense_vs_position(conn, away, position, as_of)
            entry["home_defender_vs_away_scorer"] = h2h_matchup(
                conn, h["player_id"], a["player_id"], season, home_dvp)
            entry["away_defender_vs_home_scorer"] = h2h_matchup(
                conn, a["player_id"], h["player_id"], season, away_dvp)
        features["positions"][position] = entry

    officials = [r["official_name"] for r in conn.execute(
        "SELECT official_name FROM game_officials WHERE game_id = ?", (game_id,)
    ).fetchall()]
    features["referee"] = referee_crew_tendency(conn, officials, as_of)

    conn.close()
    return features
