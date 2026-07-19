# NBA Edge Finder

A system to predict NBA game winners from recent team/player performance,
acting as our own lines-maker rather than comparing against a sportsbook.
Backtested against the 2025-26 season until results hit target.

Status: **ingestion + market-analysis code written, not yet run against
live data.** This environment's network policy currently blocks every
external host this project needs (stats.nba.com, balldontlie.io,
basketball-reference.com, the-odds-api.com, polymarket.com) — see
"Known blocker" below. Feature engineering, the model, and backtesting
(`src/features`, `src/models/win_predictor.py`, `src/backtest`) are still
empty stubs, waiting on real data to design against.

## Known blocker

This Claude Code Remote environment's egress policy denies outbound
connections to every data source this project needs. Confirmed via the
proxy status endpoint (403 policy denial on each host). Nothing here can
be tested end-to-end until the environment's network policy is updated
to allow: `stats.nba.com`, `balldontlie.io` (or `api.balldontlie.io`),
`basketball-reference.com`, `the-odds-api.com`, `polymarket.com`
(`gamma-api.polymarket.com`, `clob.polymarket.com`). See
https://code.claude.com/docs/en/claude-code-on-the-web for how
environment network policy is configured.

Because of this, the ingestion modules below are written against each
source's documented API/page structure but **not verified against real
responses** — each file's docstring flags the specific assumptions
(column names, table ids, response shape) that need checking first.

## Design decisions locked in

- **Data sources (cross-checked against each other):**
  - [`nba_api`](https://github.com/swar/nba_api) — stats.nba.com, primary source for detailed box scores / advanced stats
  - [balldontlie.io](https://www.balldontlie.io/) — free REST API, reliability cross-check
  - Basketball-Reference (scraped) — historical depth, third cross-check
  - [SportsbookReviewsOnline](https://www.sportsbookreviewsonline.com/) historical odds files — free, manually downloaded, used for **backtesting** (already has opening + closing lines)
  - [The Odds API](https://the-odds-api.com/) free tier — polled going forward to build our own line-movement history
  - [Polymarket](https://polymarket.com/) — real-money-weighted price, used as an independent check and for in-game "live money" tracking, when a market exists for the game
- **Storage:** SQLite (free, zero setup) — `src/db/schema.sql`
- **Workflow:** reusable Python package (`src/`) + notebooks for research/inspection + a daily CLI pipeline for automation. No automated bet placement — the system only recommends.

### Model design

- **Edge = team vs. team, not team vs. market.** The prediction is our
  own composite score for each team; edge is the gap between the two.
  Market odds are tracked separately, used only for (a) ROI/payout
  calculation during backtesting and (b) sharp-money/line-movement
  analysis — never as an input to the prediction itself.
- **Player signal:** each player's last 3 games, per stat, vs. their own
  season average. Expected playing time and bench role are factored in —
  a player projected for fewer/more minutes than usual (via
  `player_expected_minutes`, `player_game_stats.is_starter/status`)
  changes how much their stat delta counts toward the team, and DNP/out
  players contribute zero rather than being silently skipped.
- **Team/matchup signal:** each team's last 4 games, per stat, side-by-side
  against the opponent's last 4, adjusted for strength of schedule
  (opponent net rating) and home/away (separate home vs. road stat
  averages, not one flat bonus).
- **Win condition:** discovered from stats alone (composite scorecard —
  who wins more categories — combined with statistically-fit thresholds
  from regression), re-discovered on a rolling/expanding walk-forward
  basis through the season so nothing is fit on data it will later be
  tested against. Seeded initially from the 2025-26 season.
- **Two signals:** "Stats" (apply the win condition to tonight's
  matchup) and "Consistency" (how many of each team's last 4 games
  actually hit the win condition). Combined via a weighted blend
  (starting ~60% Stats / 40% Consistency), with the split itself tuned
  by backtest results.
- **Sharp money / line movement:** no free source publishes real
  bet-ticket percentages, so this is approximated from line movement
  itself (reverse line movement off the opening favorite, steam moves,
  and the Polymarket-vs-book gap) — see `src/market/line_movement.py`
  docstring for the exact caveats.

## Project layout

```
src/
  ingestion/       one module per data source (nba_api, balldontlie, bref,
                   sbr_historical_odds, odds_api, polymarket)
  reconciliation/  cross-checks stats across sources, logs disagreements
  market/          vig calc, line-movement/sharp-money analysis
  features/        rolling team/player performance features (empty stub)
  models/          win-probability model (empty stub) + team-vs-team edge
  backtest/        walk-forward backtest against past seasons (empty stub)
  db/              SQLite schema + connection helper
  cli/             daily automation entry point (empty stub)
data/              raw/processed data + odds snapshots (gitignored, regenerable)
notebooks/         exploration and backtest-result inspection
tests/
```

## Setup

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `ODDS_API_KEY` (free signup at
the-odds-api.com) and `BALLDONTLIE_API_KEY` (free signup at
balldontlie.io) — both required even on free tiers.

SBR historical odds files are a manual download (not an API): get the
season file from sportsbookreviewsonline.com and place it under
`data/odds/` before calling `sbr_historical_odds.load_season_odds()`.
