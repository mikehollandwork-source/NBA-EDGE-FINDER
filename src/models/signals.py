"""Turns build_matchup_features()'s nested output into the "Stats" and
"Consistency" signals designed with the user:

  - flatten_features(): every comparable stat as a single home-minus-away
    number (a "diff"), which is what both the composite scorecard and the
    regression fit operate on.
  - composite_scorecard(): how many of the diffs favor home vs. away.
  - fit_win_condition_weights(): logistic regression on historical diffs
    -> actual home win, the "statistically-fit thresholds" half of the
    win condition.
  - win_condition_score(): the two combined into one number.
  - Stats signal: win_condition_score() applied to tonight's matchup.
  - Consistency signal: how many of each team's last 4 games would have
    hit the SAME win condition (recomputed point-in-time for each of
    those past games, not just read off history).

FEATURE_GROUPS below is what makes "test different stat combinations"
possible -- callers pass a list of group names and only those diffs feed
the win condition, which is exactly the combination-testing the backtest
harness (src/backtest/run_backtest.py) sweeps over.

This module computes signals -- it still does not decide a pick. Edge
(src/models/edge.py) and any threshold-to-a-pick decision remain the
user's to design.
"""

from __future__ import annotations

from src.db.connection import get_connection
from src.features import build_features as bf

CONSISTENCY_LOOKBACK_GAMES = 4
DEFAULT_SCORECARD_WEIGHT = 0.5   # blend between composite scorecard and regression score


def _conn(db_path):
    return get_connection(db_path) if db_path else get_connection()


# --------------------------------------------------------------------------
# flatten: nested features -> named home-minus-away diffs
# --------------------------------------------------------------------------

# Every diff belongs to a group, so a caller can test combinations by
# passing a subset of group names (e.g. ["team", "playstyle"] to test
# without the h2h/position layer).
FEATURE_GROUPS: dict[str, list[str]] = {
    "team": ["points", "fg3m", "fg3a", "oreb", "dreb", "ast", "stl", "blk",
             "tov", "pf", "off_rating", "def_rating", "net_rating", "pace"],
    "sos": ["sos_net_rating"],
    "home_away_split": ["home_away_net_rating_edge"],
    "playstyle": ["three_point_rate", "assist_rate", "offensive_rebound_rate",
                  "free_throw_rate", "turnover_rate"],
    "style_clash": ["three_point_clash", "oreb_clash"],
    "composition": ["height_inches", "starter_minutes_share"],
    "rest_travel": ["rest_days", "back_to_back", "travel_miles"],
    "schedule_spot": ["letdown_lookahead_gap"],
    "bench": ["bench_points_form_delta"],
    "positions": ["position_form_delta", "position_height_advantage", "position_h2h_edge"],
}

ALL_FEATURE_GROUPS = list(FEATURE_GROUPS)


def flatten_features(features: dict, groups: list[str] | None = None) -> dict[str, float]:
    """{diff_name: home_value - away_value} for every enabled group.
    Missing/None inputs just don't produce that key (regression/
    scorecard both skip missing diffs per-game rather than erroring)."""
    groups = groups if groups is not None else ALL_FEATURE_GROUPS
    diffs: dict[str, float] = {}
    home, away = features["team"]["home"]["overall"], features["team"]["away"]["overall"]

    if "team" in groups:
        for field in FEATURE_GROUPS["team"]:
            if home.get(field) is not None and away.get(field) is not None:
                diffs[f"team_{field}"] = home[field] - away[field]

    if "sos" in groups:
        h_sos = features["team"]["home"]["sos"].get("avg_opponent_net_rating")
        a_sos = features["team"]["away"]["sos"].get("avg_opponent_net_rating")
        if h_sos is not None and a_sos is not None:
            diffs["sos_net_rating"] = h_sos - a_sos

    if "home_away_split" in groups:
        h_home = features["team"]["home"].get("home_split", {}).get("net_rating")
        a_away = features["team"]["away"].get("away_split", {}).get("net_rating")
        if h_home is not None and a_away is not None:
            diffs["home_away_net_rating_edge"] = h_home - a_away

    if "playstyle" in groups:
        h_style = features["playstyle"]["home"]["own"]
        a_style = features["playstyle"]["away"]["own"]
        for field in FEATURE_GROUPS["playstyle"]:
            if h_style.get(field) is not None and a_style.get(field) is not None:
                diffs[f"playstyle_{field}"] = h_style[field] - a_style[field]

    if "style_clash" in groups:
        h_style, a_style = features["playstyle"]["home"]["own"], features["playstyle"]["away"]["own"]
        h_allow, a_allow = features["playstyle"]["home"]["allowed"], features["playstyle"]["away"]["allowed"]
        h3, a3allow = h_style.get("three_point_rate"), a_allow.get("opp_three_point_rate_allowed")
        a3, h3allow = a_style.get("three_point_rate"), h_allow.get("opp_three_point_rate_allowed")
        if None not in (h3, a3allow, a3, h3allow):
            diffs["three_point_clash"] = (h3 - a3allow) - (a3 - h3allow)
        ho, aoallow = h_style.get("offensive_rebound_rate"), a_allow.get("opp_offensive_rebound_rate_allowed")
        ao, hoallow = a_style.get("offensive_rebound_rate"), h_allow.get("opp_offensive_rebound_rate_allowed")
        if None not in (ho, aoallow, ao, hoallow):
            diffs["oreb_clash"] = (ho - aoallow) - (ao - hoallow)

    if "composition" in groups:
        h_comp, a_comp = features["composition"]["home"], features["composition"]["away"]
        if h_comp.get("minutes_weighted_height_inches") is not None and \
           a_comp.get("minutes_weighted_height_inches") is not None:
            diffs["height_inches"] = (h_comp["minutes_weighted_height_inches"]
                                      - a_comp["minutes_weighted_height_inches"])
        if h_comp.get("starter_minutes_share") is not None and a_comp.get("starter_minutes_share") is not None:
            diffs["starter_minutes_share"] = h_comp["starter_minutes_share"] - a_comp["starter_minutes_share"]

    if "rest_travel" in groups:
        h_rt, a_rt = features["rest_travel"]["home"], features["rest_travel"]["away"]
        if h_rt.get("rest_days") is not None and a_rt.get("rest_days") is not None:
            diffs["rest_days"] = h_rt["rest_days"] - a_rt["rest_days"]
            diffs["back_to_back"] = int(bool(a_rt.get("back_to_back"))) - int(bool(h_rt.get("back_to_back")))
        if h_rt.get("travel_miles") is not None and a_rt.get("travel_miles") is not None:
            diffs["travel_miles"] = a_rt["travel_miles"] - h_rt["travel_miles"]  # more travel = worse

    if "schedule_spot" in groups:
        h_sp, a_sp = features["schedule_spot"]["home"], features["schedule_spot"]["away"]
        h_bad = (h_sp.get("letdown_gap") or 0) + (h_sp.get("lookahead_gap") or 0)
        a_bad = (a_sp.get("letdown_gap") or 0) + (a_sp.get("lookahead_gap") or 0)
        diffs["letdown_lookahead_gap"] = a_bad - h_bad  # more "bad spot" for the opponent helps us

    if "bench" in groups:
        h_bench = features["bench"]["home"].get("bench_points_form_delta")
        a_bench = features["bench"]["away"].get("bench_points_form_delta")
        if h_bench is not None and a_bench is not None:
            diffs["bench_points_form_delta"] = h_bench - a_bench

    if "positions" in groups:
        _add_position_diffs(features, diffs)

    return diffs


def _add_position_diffs(features: dict, diffs: dict) -> None:
    form_sum = form_weight = height_sum = h2h_sum = 0.0
    height_n = h2h_n = 0
    for entry in features.get("positions", {}).values():
        hf, af = entry.get("home_form"), entry.get("away_form")
        hw, aw = entry.get("home_star_weight"), entry.get("away_star_weight")
        if hf and af and hw is not None and aw is not None:
            hd, ad = hf["delta"].get("points"), af["delta"].get("points")
            if hd is not None and ad is not None:
                form_sum += hd * hw - ad * aw
                form_weight += hw + aw

        h_adv = entry.get("height_advantage_home")
        if h_adv is not None:
            height_sum += h_adv
            height_n += 1

        hd2 = entry.get("home_defender_vs_away_scorer", {}).get("points_allowed_estimate")
        ad2 = entry.get("away_defender_vs_home_scorer", {}).get("points_allowed_estimate")
        if hd2 is not None and ad2 is not None:
            # lower points allowed by home's defender = good for home
            h2h_sum += ad2 - hd2
            h2h_n += 1

    if form_weight:
        diffs["position_form_delta"] = form_sum / form_weight
    if height_n:
        diffs["position_height_advantage"] = height_sum / height_n
    if h2h_n:
        diffs["position_h2h_edge"] = h2h_sum / h2h_n


# --------------------------------------------------------------------------
# composite scorecard
# --------------------------------------------------------------------------

def composite_scorecard(diffs: dict[str, float]) -> float:
    """(categories home wins - categories away wins) / total categories,
    in [-1, 1]. Ties (diff == 0) count toward neither side."""
    if not diffs:
        return 0.0
    home_wins = sum(1 for v in diffs.values() if v > 0)
    away_wins = sum(1 for v in diffs.values() if v < 0)
    return (home_wins - away_wins) / len(diffs)


# --------------------------------------------------------------------------
# regression-fit weights
# --------------------------------------------------------------------------

def fit_win_condition_weights(samples: list[tuple[dict, bool]], feature_names: list[str]):
    """Logistic regression: diffs -> home_won. samples = [(diffs, home_won), ...].
    Missing diffs are imputed as 0 (neutral) for that sample/feature.
    Returns a fitted sklearn model, or None if there's not enough data/
    variation to fit (e.g. early season, or a lopsided small sample) --
    callers must fall back to the composite scorecard alone in that case."""
    if len(samples) < 20:
        return None
    from sklearn.linear_model import LogisticRegression

    X = [[diffs.get(f, 0.0) for f in feature_names] for diffs, _ in samples]
    y = [1 if won else 0 for _, won in samples]
    if len(set(y)) < 2:
        return None  # all same outcome -- regression can't fit a boundary

    model = LogisticRegression(max_iter=1000)
    model.fit(X, y)
    return model


def win_condition_score(diffs: dict[str, float], feature_names: list[str],
                        regression_model=None, scorecard_weight: float = DEFAULT_SCORECARD_WEIGHT) -> float:
    """Blend of the composite scorecard (in [-1,1]) and the regression
    model's home-win probability (rescaled to [-1,1]) -- the "win
    condition" applied to one matchup's diffs. Falls back to scorecard-
    only when no regression model is available yet (e.g. early season)."""
    scorecard = composite_scorecard(diffs)
    if regression_model is None:
        return scorecard

    x = [[diffs.get(f, 0.0) for f in feature_names]]
    home_win_prob = regression_model.predict_proba(x)[0][1]
    regression_score = 2 * home_win_prob - 1  # rescale [0,1] -> [-1,1]
    return scorecard_weight * scorecard + (1 - scorecard_weight) * regression_score


# --------------------------------------------------------------------------
# consistency: hit-rate of the win condition over each team's recent games
# --------------------------------------------------------------------------

def team_consistency(conn, team: str, as_of: str, feature_names: list[str],
                     regression_model=None, hit_threshold: float = 0.0,
                     n_games: int = CONSISTENCY_LOOKBACK_GAMES, db_path=None) -> dict:
    """How many of `team`'s last n_games would have hit the win condition
    (recomputed point-in-time for each past game -- not read off whether
    they actually won, this measures whether the STATISTICAL condition
    was met, per the original design). db_path must match whatever `conn`
    is already open on -- build_matchup_features opens its OWN connection
    internally, so passing the wrong path here would silently read/write
    a different database than the rest of this computation."""
    recent = bf.team_recent_games(conn, team, as_of, n_games)
    hits = 0
    scored = 0
    for game_row in recent:
        try:
            feats = bf.build_matchup_features(game_row["game_id"], db_path=db_path, as_of=game_row["game_date"])
        except Exception:
            continue
        diffs = flatten_features(feats)
        score = win_condition_score(diffs, feature_names, regression_model)
        team_score = score if feats["home_team"] == team else -score
        if team_score > hit_threshold:
            hits += 1
        scored += 1
    return {"hits": hits, "games_scored": scored,
           "hit_rate": (hits / scored) if scored else None}


# --------------------------------------------------------------------------
# top-level: Stats + Consistency signals for one matchup
# --------------------------------------------------------------------------

def compute_signals(game_id: str, features: dict | None = None,
                    feature_names: list[str] | None = None,
                    regression_model=None, scorecard_weight: float = DEFAULT_SCORECARD_WEIGHT,
                    stats_weight: float = 0.6, db_path=None) -> dict:
    """Stats signal + Consistency signal for both teams, combined into a
    composite score and the team-vs-team edge. feature_names controls
    which diffs count -- this is the knob the backtest harness turns to
    test different stat combinations. Pass `features` if the caller
    already built them (e.g. the hourly pipeline) to skip rebuilding."""
    conn = _conn(db_path)
    feature_names = feature_names or [n for names in FEATURE_GROUPS.values() for n in names]

    feats = features or bf.build_matchup_features(game_id, db_path=db_path)
    diffs = flatten_features(feats)
    home_stats = win_condition_score(diffs, feature_names, regression_model, scorecard_weight)
    away_stats = -home_stats

    home_consistency = team_consistency(conn, feats["home_team"], feats["as_of"],
                                        feature_names, regression_model, db_path=db_path)
    away_consistency = team_consistency(conn, feats["away_team"], feats["as_of"],
                                        feature_names, regression_model, db_path=db_path)
    home_cons_score = (home_consistency["hit_rate"] or 0.5) * 2 - 1  # rescale [0,1] -> [-1,1]
    away_cons_score = (away_consistency["hit_rate"] or 0.5) * 2 - 1

    home_composite = stats_weight * home_stats + (1 - stats_weight) * home_cons_score
    away_composite = stats_weight * away_stats + (1 - stats_weight) * away_cons_score

    conn.close()
    return {
        "game_id": game_id, "home_team": feats["home_team"], "away_team": feats["away_team"],
        "home_stats_signal": home_stats, "away_stats_signal": away_stats,
        "home_consistency": home_consistency, "away_consistency": away_consistency,
        "home_consistency_signal": home_cons_score, "away_consistency_signal": away_cons_score,
        "home_composite_score": home_composite, "away_composite_score": away_composite,
        "edge": home_composite - away_composite,
        "predicted_winner": feats["home_team"] if home_composite > away_composite else feats["away_team"],
    }
