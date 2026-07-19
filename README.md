# NBA Edge Finder

A system to predict NBA game winners from recent team/player performance
and find betting edges by comparing model probabilities against market
odds. Backtested against last season until results hit target.

Status: **skeleton only** — no ingestion, feature, or model logic has
been implemented yet. Structure exists so the shape of the project is
visible before it's filled in; everything below describes the plan,
not finished code.

## Design decisions locked in so far

- **Data sources (cross-checked against each other):**
  - [`nba_api`](https://github.com/swar/nba_api) — stats.nba.com, primary source for detailed box scores / advanced stats
  - [balldontlie.io](https://www.balldontlie.io/) — free REST API, reliability cross-check
  - Basketball-Reference (scraped) — historical depth, third cross-check
  - [SportsbookReviewsOnline](https://www.sportsbookreviewsonline.com/) historical odds files — free, used for **backtesting** (already contains opening + closing lines for past seasons)
  - [The Odds API](https://the-odds-api.com/) free tier — used **going forward** to poll and snapshot live odds ourselves, since no free source retains historical line movement in real time
- **Storage:** SQLite (free, zero setup) — see `src/db/schema.sql`
- **Workflow:** reusable Python package (`src/`) + notebooks for research/inspection + a daily CLI pipeline for automation. No automated bet placement — the system only recommends.
- **Backtest target:** tracked weekly — accuracy, ROI, and calibration — iterated until target results are hit.

## Open / in progress

- Exact model design (features, algorithm, how edge is computed) — being defined with the user before implementation.

## Project layout

```
src/
  ingestion/       one module per data source
  reconciliation/  cross-checks stats across sources, logs disagreements
  features/        rolling team/player performance features
  models/          win-probability model + edge-vs-market calculation
  backtest/        walk-forward backtest against past seasons
  db/              SQLite schema + connection helper
  cli/             daily automation entry point
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

Copy `.env.example` to `.env` and fill in `ODDS_API_KEY` (free tier signup
at the-odds-api.com) once odds polling is implemented.
