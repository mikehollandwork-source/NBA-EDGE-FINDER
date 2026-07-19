"""Vig (bookmaker overround) calculation and de-vigging of moneylines.

Used for ROI/payout calculation and for the line-movement analysis --
NOT for the core prediction, which is team-vs-team (see src/models/edge.py).
"""


def american_to_implied_prob(moneyline: int) -> float:
    """Raw (vig-included) implied probability from American odds."""
    if moneyline > 0:
        return 100 / (moneyline + 100)
    return -moneyline / (-moneyline + 100)


def compute_vig(home_moneyline: int, away_moneyline: int) -> float:
    """The bookmaker's overround, e.g. 0.045 = 4.5% vig.

    Sum of raw implied probabilities minus 1 -- a fair (no-vig) market
    would sum to exactly 1.0.
    """
    home_prob = american_to_implied_prob(home_moneyline)
    away_prob = american_to_implied_prob(away_moneyline)
    return (home_prob + away_prob) - 1.0


def remove_vig(home_moneyline: int, away_moneyline: int) -> tuple[float, float]:
    """De-vigged (home_prob, away_prob) that sum to 1.0.

    Simple proportional (multiplicative) de-vig: scale each raw implied
    probability down by the total overround. This is the standard
    approach for two-outcome moneylines; more sophisticated de-vig
    methods (e.g. Shin's method) exist but assume more than we need
    here since edge no longer depends on market probability at all --
    this is only for ROI/payout math.
    """
    home_prob = american_to_implied_prob(home_moneyline)
    away_prob = american_to_implied_prob(away_moneyline)
    total = home_prob + away_prob
    return home_prob / total, away_prob / total
