"""Edge calculation: team-vs-team, NOT team-vs-market.

Per the agreed design, "edge" is the gap between the two teams' own
composite scores (Stats signal + Consistency signal), not a comparison
to sportsbook implied probability. Market odds are still tracked
elsewhere (src/market/) but only for ROI/payout calculation and
sharp-money analysis, never as an input to this.
"""


def compute_edge(home_composite_score: float, away_composite_score: float) -> float:
    """Positive = home favored, negative = away favored, magnitude = edge size."""
    return home_composite_score - away_composite_score


def predicted_winner(home_team: str, away_team: str, edge: float) -> str:
    return home_team if edge > 0 else away_team
