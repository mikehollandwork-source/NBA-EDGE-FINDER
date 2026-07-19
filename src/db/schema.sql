-- NBA Edge Finder database schema (SQLite)
--
-- DRAFT: this is a starting proposal, not final. Column lists for
-- team_game_stats / player_game_stats will be extended once the feature
-- design (task: "design model with user") is settled.

-- One row per game, per the NBA's own schedule/results.
CREATE TABLE IF NOT EXISTS games (
    game_id         TEXT PRIMARY KEY,   -- source-agnostic id we assign
    season          TEXT NOT NULL,      -- e.g. '2023-24'
    game_date       TEXT NOT NULL,      -- ISO date
    home_team       TEXT NOT NULL,      -- team abbreviation
    away_team       TEXT NOT NULL,
    home_score      INTEGER,
    away_score      INTEGER,
    status          TEXT NOT NULL       -- 'scheduled' | 'final'
);

-- Same game's team-level box score as reported by each source separately,
-- so reconciliation can compare them instead of trusting one blindly.
CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    team            TEXT NOT NULL,
    source          TEXT NOT NULL,      -- 'nba_api' | 'balldontlie' | 'bref'
    points          INTEGER,
    -- additional columns TBD with feature design
    PRIMARY KEY (game_id, team, source)
);

-- Same idea at player level.
CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    player_id       TEXT NOT NULL,
    team            TEXT NOT NULL,
    source          TEXT NOT NULL,
    minutes         REAL,
    points          INTEGER,
    -- additional columns TBD with feature design
    PRIMARY KEY (game_id, player_id, source)
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

-- Odds snapshots: for historical (backtest) games these come from the SBR
-- historical files (open + close already present). For live/going-forward
-- games these accumulate over time as we poll the Odds API on a schedule.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    source          TEXT NOT NULL,      -- 'sbr_historical' | 'odds_api'
    book            TEXT,               -- sportsbook name, if known
    snapshot_time   TEXT NOT NULL,      -- when this line was observed
    line_type       TEXT NOT NULL,      -- 'opening' | 'live' | 'closing'
    home_moneyline  INTEGER,
    away_moneyline  INTEGER,
    home_spread     REAL,
    total            REAL
);

-- Model predictions, versioned so backtests are comparable across changes.
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT NOT NULL REFERENCES games(game_id),
    model_version   TEXT NOT NULL,
    generated_at    TEXT NOT NULL,
    home_win_prob   REAL NOT NULL,
    market_home_win_prob REAL,          -- de-vigged, from odds_snapshots
    edge            REAL                -- home_win_prob - market_home_win_prob
);

-- Aggregate backtest results per run, so iterations are tracked over time.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version   TEXT NOT NULL,
    season          TEXT NOT NULL,
    run_at          TEXT NOT NULL,
    accuracy        REAL,
    roi             REAL,
    calibration_error REAL,
    notes           TEXT
);
