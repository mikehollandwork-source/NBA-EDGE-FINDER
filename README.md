# NBA Edge Finder

A system to predict NBA game winners from recent team/player performance,
acting as our own lines-maker rather than comparing against a sportsbook.
Backtested against the 2025-26 season until results hit target.

Status: **full pipeline built and run against real live infrastructure
via GitHub Actions.** Several real bugs were found and fixed this way
(nba_api timeout/retry tuning, balldontlie partial-commit handling,
pandas 3.0's `read_html` API change, a numpy-scalar/sqlite BLOB-storage
bug) — see "Known blocker" below for the big one: **stats.nba.com is
confirmed unreachable from GitHub Actions.**

**The full pipeline is built end-to-end**: feature engineering
(`src/features/build_features.py` — team rate/advanced stats, SOS,
position-by-position h2h, star-weighted form, playstyle, composition,
rest/travel, referees, schedule-spot risk), the win-condition/signal
layer (`src/models/signals.py` — composite scorecard + regression-fit
win condition, Stats + Consistency signals, all with toggleable stat
combinations), `src/models/win_predictor.py` (writes predictions), and
`src/backtest/run_backtest.py` (walk-forward backtest with ROI, ranking
several stat combinations against each other).

## Known blocker: stats.nba.com is unreachable from GitHub Actions

Three live backtest runs against real GitHub Actions infrastructure
progressively surfaced this: **every single request to stats.nba.com
from a GitHub Actions runner hangs to the full timeout with zero
response** — confirmed with a diagnostic that bypassed the `nba_api`
library entirely and hit the host directly (`src/cli/diagnose_nba_api.py`,
`.github/workflows/diagnose_nba_api.yml`). A baseline request to an
unrelated host succeeded instantly in the same run, ruling out a general
network problem. This is the signature of stats.nba.com blocking/
dropping traffic from GitHub Actions' cloud IP range — not a timeout,
retry, or payload-size problem, and not something fixable from this
project's code.

**Consequence — primary source switched to basketball-reference:**
`nba_api` was the original PRIMARY source (real player h2h matchups,
precise advanced ratings, speed/distance tracking, reliable positions).
Since it can't run unattended on GitHub Actions, `basketball_reference_source.py`
now supplies team + player box scores, advanced stats (`ORtg`/`DRtg` per
player, verified against a real live page), officials, and roster
positions — every `build_features.py` query filters on `source='bref'`
(see that module's docstring). `nba_api` is now **best-effort and OFF by
default**: `backfill_season.py --include-nba-api` still pulls it, but
only useful run from a machine with a non-cloud IP (e.g. locally) —
`balldontlie` discovers games/schedule either way, so a fully-offline-
from-nba_api backfill still works.

**What's lost with this switch** (no free replacement exists for
either): real player-vs-player h2h matchup data (falls back to the
defense-vs-position estimate only, which the design already treats as a
fallback tier — just never the *only* tier before) and SportVU speed/
distance tracking.

Because basketball-reference has no official API, the ingestion modules
below are written against the page structures verified live during this
project's debugging session (box score table ids/columns, officials
text, response encoding) — flagged per-function where verified vs.
still-assumed (the roster/position page specifically wasn't diagnosed
live, only the box score page was; spot-check it on the first real run).

## Design decisions locked in

- **Stat sources (cross-checked against each other):**
  - Basketball-Reference (scraped) — **primary source** for team/player box scores, advanced stats, officials, and roster positions, since it's the only one of the three confirmed reachable from GitHub Actions (see "Known blocker" above)
  - [balldontlie.io](https://www.balldontlie.io/) — free REST API, primary game/schedule discoverer (games table) + reliability cross-check
  - [`nba_api`](https://github.com/swar/nba_api) — stats.nba.com, richer data (real h2h matchups, precise ratings, speed/distance) but confirmed unreachable from GitHub Actions; best-effort/off by default, opt in with `--include-nba-api` when running locally
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

## Running the season backtest

This is a separate, on-demand workflow (`.github/workflows/backtest.yml`)
from the hourly job — it's a slow, one-off run, not something to
schedule. Trigger it manually from the GitHub Actions tab:

1. **First time only**: before triggering, download the 2025-26 season's
   file from sportsbookreviewsonline.com and commit it to `data/odds/`
   (any `.xlsx` file there gets picked up automatically). Skippable —
   the backtest still runs and reports accuracy without it, just not ROI.
2. Run the **"Season Backtest"** workflow with the season input (default
   `2025-26`). First run does a full backfill (pulls the whole season —
   this is the slow part, expect it to take a while) then the
   walk-forward backtest across several stat combinations
   (`src/backtest/run_backtest.py`'s `CURATED_COMBOS`); subsequent runs
   can check "skip backfill" to just re-run the backtest against
   whatever's already in the committed database.
3. Results land in `data/backtest_results_<season>.json` (committed back
   to the repo) and in the `backtest_runs` table — accuracy, ROI, and
   games graded per combination, so you can see which stat combinations
   actually would have made money.

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
  features/        build_features.py -- the full feature layer
  models/          signals.py (win condition + Stats/Consistency signals),
                     win_predictor.py (writes predictions), edge.py
  backtest/        run_backtest.py -- walk-forward + combination-testing
  db/              SQLite schema + connection helper
  cli/             hourly_pipeline.py (hourly automation),
                     backfill_season.py (one-time full-season pull)
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
