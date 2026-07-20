# NBA Edge Finder

A system to predict NBA game winners from recent team/player performance,
acting as our own lines-maker rather than comparing against a sportsbook.
Backtested against the 2025-26 season until results hit target.

Status: **ingestion, market-analysis, and hourly automation code
written, not yet run against live data.** The Claude Code session that
built this is behind a network policy that blocks every external host
the project needs (stats.nba.com, balldontlie.io,
basketball-reference.com, the-odds-api.com, polymarket.com) — see
"Known blocker" below. The GitHub Actions workflow itself runs on
GitHub's own infrastructure and is NOT behind that block, so once the
two setup steps in "Automation" below are done, the hourly job should
run for real even though it couldn't be tested from the Claude session.
Feature engineering (`src/features/build_features.py`) is now built —
team rate/advanced stats (SOS + home/away adjusted), position-by-position
starter comparisons (real h2h matchup data blended with a self-derived
defense-vs-position fallback), star-weighted player form, bench
contribution, height, rest/travel, referee tendency, and expected pace.
Verified end-to-end against synthetic data (caught and fixed a real SQL
bug this way — an aggregate query missing `GROUP BY` was collapsing all
5 positions into one) since live sources are still blocked here. The
model itself and backtesting (`src/models/win_predictor.py`,
`src/backtest`) are still empty, waiting on the win-condition/signal
design.

## Known blocker

This Claude Code Remote environment's egress policy denies outbound
connections to every data source this project needs. Confirmed via the
proxy status endpoint (403 policy denial on each host). Nothing here can
be tested end-to-end from this session — but note the GitHub Actions
workflow runs on GitHub's own infrastructure and is NOT behind this
block, so the hourly job should work for real regardless (see
"Automation"). Hosts involved, if you do want to unblock this session
too: `stats.nba.com`, `balldontlie.io` (or `api.balldontlie.io`),
`basketball-reference.com`, `the-odds-api.com`, `polymarket.com`
(`gamma-api.polymarket.com`, `clob.polymarket.com`), `espn.com`
(`site.api.espn.com`, `sports.core.api.espn.com`), `pinnacle.com`
(`guest.api.arcadia.pinnacle.com`), `kalshi.com`
(`api.elections.kalshi.com`), `covers.com`, `scoresandodds.com`,
`vsin.com` (`data.vsin.com`), `reddit.com`, `wikimedia.org`. See
https://code.claude.com/docs/en/claude-code-on-the-web for how
environment network policy is configured.

Because of this, the ingestion modules below are written against each
source's documented API/page structure but **not verified against real
responses** — each file's docstring flags the specific assumptions
(column names, table ids, response shape) that need checking first.

## Design decisions locked in

- **Stat sources (cross-checked against each other):**
  - [`nba_api`](https://github.com/swar/nba_api) — stats.nba.com, primary source for detailed box scores / advanced stats
  - [balldontlie.io](https://www.balldontlie.io/) — free REST API, reliability cross-check
  - Basketball-Reference (scraped) — historical depth, third cross-check
- **Market/odds sources, all free and no signup except where noted:**
  - [ESPN](https://www.espn.com/) hidden scoreboard/odds API — real open→current moneyline for nearly every game, no key. Primary line source.
  - [Pinnacle](https://www.pinnacle.com/) guest API — the "sharp book" reference line.
  - [The Odds API](https://the-odds-api.com/) free tier — optional now that ESPN covers the same ground for free; only runs if `ODDS_API_KEY` is set.
  - [SportsbookReviewsOnline](https://www.sportsbookreviewsonline.com/) historical odds files — free, manually downloaded, used for **backtesting** (already has opening + closing lines).
  - [Polymarket](https://polymarket.com/) and [Kalshi](https://kalshi.com/) — two independent real-money prediction markets, used as a cross-check on book prices and for in-game "live money" tracking, when a market exists for the game.
- **Public-sentiment sources, for cross-checking "who the public/sharp money is on" (raw data only — see "Pick logic" below):**
  - [covers.com](https://www.covers.com/) — published consensus % + betting-forum team-mention tally.
  - [Scores & Odds](https://www.scoresandodds.com/) and [VSIN](https://www.vsin.com/) — both publish **both** ticket share (% of bets) and dollar share (% of money) per game; the bets-vs-money divergence is a real sharp-money tell, not an approximation.
  - Reddit (r/nba + betting subreddits) — second forum-mention tally, same technique as covers'.
  - Wikipedia pageviews — team-attention proxy; fully backtestable (real history via the Wikimedia API), unlike the other sentiment sources.
- **Storage:** SQLite (free, zero setup) — `src/db/schema.sql`
- **Workflow:** reusable Python package (`src/`) + notebooks for research/inspection + an hourly CLI pipeline for automation. No automated bet placement — the system only recommends.

Most of the market/public-sentiment sources above were ported from a
sibling MLB project (`mikehollandwork-source/Sports`) that has already
run these scrapers live and fixed their selectors/endpoints against real
responses — see each module's docstring for what's a direct port vs. an
NBA-specific best-effort guess (team names/URLs swapped, unverified until
the first live run here).

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
- **Sharp money / line movement:** `src/market/line_movement.py`'s
  reverse-line-movement/steam-move detection is still an approximation
  (no free source publishes real bet-ticket percentages *from a
  sportsbook's own moneyline data*). But Scores & Odds and VSIN
  (`public_odds_sources.py`) DO publish real ticket% vs money% splits —
  that's a genuine sharp-vs-public signal, stored per-source in
  `public_sentiment_snapshots` for the pick logic to use directly rather
  than inferred from price movement alone.

### Pick logic — deliberately not built here

Everything above is data collection and raw signal storage. The actual
decision logic (how Stats/Consistency/public-sentiment signals combine
into a pick) is being designed separately and is NOT ported from the MLB
sibling project — that project's decision engine (`analysis.py`) is built
around baseball-specific stats (FIP, wOBA) and a "fade the public" thesis
that doesn't apply to this project's team-vs-team edge design.
`src/models/win_predictor.py` stays an empty stub until that design is
finalized.
- **Odds timestamps**: the hourly job seeds `games` rows for the next 7
  days (via balldontlie, which returns unplayed games) specifically so
  early lines have somewhere to attach — every hourly poll writes a new
  `odds_snapshots` row regardless of whether the price moved, so the
  full history from whenever a book first posts a line through closing
  is all there, one row per hour, not just opening/closing bookends.

## Automation

A GitHub Actions workflow (`.github/workflows/hourly_pipeline.yml`) runs
`src/cli/hourly_pipeline.py` every hour: pulls the last few days of
results, cross-checks sources, snapshots odds + Polymarket, settles any
completed predictions against the record, and sends a Telegram summary
(today / yesterday / this week / this month / YTD, win-loss and units)
when something actually changed since the last run.

**Two setup steps only you can do:**

1. **GitHub Actions secrets** (repo Settings → Secrets and variables →
   Actions): `BALLDONTLIE_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID` are what the pipeline actually needs today.
   `ODDS_API_KEY` is optional (ESPN/Pinnacle cover odds for free now).
   `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` are optional (Reddit tally
   degrades to zero without them). Don't share these values in chat or
   commit them anywhere — they only need to exist as secrets.
2. **Telegram bot**: message `@BotFather` on Telegram, `/newbot`, follow
   the prompts for a bot token. Then message your new bot anything and
   visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your
   chat_id in the response.

**Persistence default**: since GitHub Actions runners are wiped after
every run, the workflow commits `data/nba_edge_finder.db` back to the
repo each time (simplest free option — revisit if the binary diffs
become annoying, e.g. by switching to committing raw CSVs and rebuilding
the DB each run instead).

**Staking/settlement defaults** (both easy to change in
`src/tracking/record.py` once you have an opinion): flat 1 unit per
pick, settled against the closing moneyline available at settlement
time — not the price live when the prediction was actually generated.

## Project layout

```
src/
  ingestion/       one module per data source:
                     nba_api, balldontlie, basketball_reference  (stats)
                     espn, pinnacle, odds_api, sbr_historical_odds,
                       polymarket, kalshi                        (odds/markets)
                     covers, public_odds_sources (S&O + VSIN),
                       reddit, wiki                               (public sentiment)
                     public_sentiment_collector                   (orchestrates the above)
                     nba_teams                                    (shared abbr/name lookup)
  reconciliation/  cross-checks stats across sources, logs disagreements
  market/          vig calc, line-movement/sharp-money analysis
  tracking/        settles predictions, computes the units/win-loss rollup
  notify/          Telegram summary sender
  features/        rolling team/player performance features (empty stub)
  models/          win-probability model (empty stub) + team-vs-team edge
  backtest/        walk-forward backtest against past seasons (empty stub)
  db/              SQLite schema + connection helper
  cli/             hourly automation entry point (runs the full pipeline)
data/              raw/processed data + odds snapshots + the committed db
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
