-- NBA Edge Finder database schema (SQLite)
--
-- Reflects the design agreed with the user:
--   - edge = our own team-vs-team signal comparison, NOT vs. market odds
--   - market odds are still tracked, but only for (a) computing ROI/payout
--     during backtesting and (b) sharp-money / line-movement analysis
--   - "Stats" signal (side-by-side team comparison, last 4 games, SOS +
--     home/away adjusted) and "Consistency" signal (hit-rate of the
--     discovered win condition over each team's last 4 games) are the two
--     inputs to the final composite score
--   - player-level signal uses a 3-game window vs. that player's season
--     average; expected playing time / bench role factor into how much
--     each player's delta counts (see player_status, player_game_stats)

-- One row per game, per the NBA's own schedule/results.
CREATE TABLE IF NOT EXISTS games (
    game_id         TEXT PRIMARY KEY,   -- source-agnostic id we assign
    season          TEXT NOT NULL,      -- e.g. '2025-26'
    game_date       TEXT NOT NULL,      -- ISO date
    home_team       TEXT NOT NULL,      -- team abbreviation
    away_team       TEXT NOT NULL,
    home_score      INTEGER,
    away_score      INTEGER,
    status          TEXT NOT NULL       -- 'scheduled' | 'final'
);

-- Same game's team-level box score as reported by each source separately,
-- so reconciliation can compare them instead of trusting one blindly.
-- Advanced fields (off/def rating, pace) let us compute strength of
-- schedule (opponent net rating) during feature engineering.
CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    team            TEXT NOT NULL,
    source          TEXT NOT NULL,      -- 'nba_api' | 'balldontlie' | 'bref'
    is_home         INTEGER NOT NULL,   -- 1 = home, 0 = away
    points          INTEGER,
    fgm             INTEGER,
    fga             INTEGER,
    fg3m            INTEGER,
    fg3a            INTEGER,
    ftm             INTEGER,
    fta             INTEGER,
    oreb            INTEGER,
    dreb            INTEGER,
    reb             INTEGER,
    ast             INTEGER,
    stl             INTEGER,
    blk             INTEGER,
    tov             INTEGER,
    pf              INTEGER,
    possessions     REAL,               -- estimated, for pace/rating calcs
    off_rating      REAL,
    def_rating      REAL,
    net_rating      REAL,
    pace            REAL,
    PRIMARY KEY (game_id, team, source)
);

-- Same idea at player level. is_starter / status feed the "expected
-- playing time" weighting; a player ruled out or DNP contributes 0 to
-- that game's team aggregate rather than being silently skipped.
CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    player_id       TEXT NOT NULL,
    player_name     TEXT,
    team            TEXT NOT NULL,
    source          TEXT NOT NULL,
    is_home         INTEGER NOT NULL,
    is_starter      INTEGER,            -- 1/0, null if unknown
    status          TEXT,               -- 'active' | 'out' | 'questionable' | 'dnp' ...
    minutes         REAL,
    points          INTEGER,
    fgm             INTEGER,
    fga             INTEGER,
    fg3m            INTEGER,
    fg3a            INTEGER,
    ftm             INTEGER,
    fta             INTEGER,
    oreb            INTEGER,
    dreb            INTEGER,
    reb             INTEGER,
    ast             INTEGER,
    stl             INTEGER,
    blk             INTEGER,
    tov             INTEGER,
    pf              INTEGER,
    plus_minus      REAL,
    PRIMARY KEY (game_id, player_id, source)
);

-- Pre-game projection of how many minutes a player is expected to play,
-- distinct from minutes actually played (player_game_stats.minutes) --
-- this is what "expected playing time" features are built from, derived
-- from recent-minutes trend adjusted by that game's reported status.
CREATE TABLE IF NOT EXISTS player_expected_minutes (
    game_id             TEXT NOT NULL REFERENCES games(game_id),
    player_id           TEXT NOT NULL,
    team                TEXT NOT NULL,
    projected_at        TEXT NOT NULL,
    expected_minutes     REAL,
    projection_basis     TEXT,          -- e.g. 'last3_avg_adjusted_for_status'
    PRIMARY KEY (game_id, player_id)
);

-- Any mismatch found between sources for the same game gets logged here
-- rather than silently resolved, so it stays visible.
CREATE TABLE IF NOT EXISTS reconciliation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    field           TEXT NOT NULL,
    source_a        TEXT NOT NULL,
    value_a         TEXT,
    source_b        TEXT NOT NULL,
    value_b         TEXT,
    detected_at     TEXT NOT NULL
);

-- Market snapshots: sportsbook moneylines (SBR historical for backtesting,
-- Odds API for live going forward) AND Polymarket implied probabilities,
-- all in one table since they're all "what did the market think at time X".
-- Used for ROI/payout calculation and for sharp-money/line-movement
-- analysis -- NOT for the core prediction edge, which is team-vs-team.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id             TEXT NOT NULL REFERENCES games(game_id),
    source              TEXT NOT NULL,   -- 'sbr_historical' | 'odds_api' | 'polymarket'
    book                TEXT,             -- sportsbook name, or 'polymarket'
    snapshot_time       TEXT NOT NULL,    -- when this line/price was observed
    line_type           TEXT NOT NULL,    -- 'opening' | 'live' | 'closing'
    home_moneyline      INTEGER,          -- American odds, null for polymarket
    away_moneyline      INTEGER,
    home_spread         REAL,
    total                REAL,
    home_implied_prob   REAL,             -- de-vigged (books) or raw (polymarket)
    away_implied_prob   REAL
);

-- Computed sharp-money / line-movement indicators between snapshots for
-- a game, kept separate from the raw snapshots so the inference logic
-- can change without re-fetching data.
CREATE TABLE IF NOT EXISTS line_movement_signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                 TEXT NOT NULL REFERENCES games(game_id),
    computed_at             TEXT NOT NULL,
    opening_vig             REAL,
    closing_vig             REAL,
    line_move_home_cents    REAL,   -- closing implied prob - opening implied prob (home)
    reverse_line_movement   INTEGER,  -- 1 if line moved opposite the presumed public side
    steam_move              INTEGER,  -- 1 if a sudden simultaneous move was detected
    polymarket_vs_book_gap  REAL,     -- polymarket implied prob - book implied prob, if both exist
    notes                   TEXT
);

-- Public-sentiment reads from every free cross-check source: covers.com
-- consensus (real bet %) + forum tally, Scores & Odds and VSIN (both give
-- ticket % AND money % -- the bets-vs-money divergence is a genuine sharp
-- tell, not an approximation), Reddit forum tally, Wikipedia pageview
-- attention, and Polymarket/Kalshi's real-money implied %. One row per
-- source per snapshot so disagreement between sources is visible rather
-- than collapsed into a single number.
CREATE TABLE IF NOT EXISTS public_sentiment_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    source          TEXT NOT NULL,   -- 'covers_consensus' | 'covers_forum' |
                                      -- 'scoresandodds_bets' | 'scoresandodds_money' |
                                      -- 'vsin_bets' | 'vsin_money' | 'reddit' |
                                      -- 'wiki' | 'polymarket_consensus' | 'kalshi_consensus'
    snapshot_time   TEXT NOT NULL,
    value_type      TEXT NOT NULL,   -- 'pct' | 'mention_count' | 'pageviews'
    home_value      REAL,
    away_value      REAL
);

-- Model predictions, versioned so backtests are comparable across changes.
-- home_win_prob/edge come purely from our own Stats+Consistency signals;
-- market_home_implied_prob is stored alongside only for ROI calculation
-- and post-hoc comparison, not because the model uses it as an input.
CREATE TABLE IF NOT EXISTS predictions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id                 TEXT NOT NULL REFERENCES games(game_id),
    model_version           TEXT NOT NULL,
    generated_at            TEXT NOT NULL,
    home_stats_signal       REAL,
    away_stats_signal       REAL,
    home_consistency_signal REAL,
    away_consistency_signal REAL,
    home_composite_score    REAL,
    away_composite_score    REAL,
    edge                    REAL,        -- home_composite_score - away_composite_score
    predicted_winner        TEXT,
    home_win_prob           REAL,        -- optional, for calibration tracking
    market_home_implied_prob REAL        -- from odds_snapshots, for ROI only
);

-- Aggregate backtest results per run, so iterations are tracked over time.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version       TEXT NOT NULL,
    season              TEXT NOT NULL,
    run_at              TEXT NOT NULL,
    accuracy            REAL,
    roi                 REAL,
    calibration_error   REAL,
    notes               TEXT
);

-- One row per settled (completed + predicted) game, for the live
-- units/win-loss record reported to Telegram. Default staking is flat
-- 1 unit per pick (simplest convention; revisit if you want edge-sized
-- staking later), settled against the closing line available at
-- settlement time (also a default -- revisit once we're tracking odds
-- at the actual moment a prediction was generated).
CREATE TABLE IF NOT EXISTS bet_record (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id             TEXT NOT NULL REFERENCES games(game_id),
    game_date           TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    predicted_winner    TEXT NOT NULL,
    actual_winner       TEXT,
    result              TEXT NOT NULL,   -- 'win' | 'loss' | 'push' | 'pending'
    units_risked        REAL NOT NULL,
    units_won           REAL,            -- negative for a loss, null while pending
    settled_at          TEXT,
    UNIQUE(game_id, model_version)
);
