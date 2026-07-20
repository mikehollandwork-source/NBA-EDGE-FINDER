"""Ingestion from Basketball-Reference via scraping (no official API).

Used for historical depth and advanced metrics, and as a third
cross-check source. No official rate limit is published; be
conservative (delay between requests, and cache pages locally if this
starts running frequently) to avoid getting blocked.

Confirmed against the real installed pandas version (3.0.3): pd.read_html
no longer accepts a bare HTML string -- it tries to open() the string
AS A FILE PATH and raises FileNotFoundError. Every call here wraps the
HTML in io.StringIO() first, verified locally against real pandas
behavior (not assumed) after this broke the first live backfill run.

Table structure (ids 'schedule', 'tgl_basic') otherwise reflects the
site's known layout, but Basketball-Reference does change table
ids/columns occasionally -- still worth spot-checking against real
output.

IMPORTANT cross-source caveat: Basketball-Reference has its own game
ids (date + home team, e.g. '202602010LAL') and its own team
abbreviations that differ from nba.com's for a few teams (BRK vs BKN,
PHO vs PHX, CHO vs CHA). There is no shared game_id across sources, so
reconciliation must match games by (date, home_team, away_team) after
normalizing abbreviations through TEAM_ABBR_TO_NBA below, not by id.
"""

import io
import logging
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.db.connection import get_connection

BASE_URL = "https://www.basketball-reference.com"
REQUEST_DELAY_SECONDS = 2.0  # conservative: no official rate limit published
SOURCE_NAME = "bref"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script; contact via repo owner)"}

log = logging.getLogger("basketball_reference_source")

# Only the abbreviations that actually differ from nba.com's.
TEAM_ABBR_TO_NBA = {
    "BRK": "BKN",
    "PHO": "PHX",
    "CHO": "CHA",
}
NBA_TO_BREF_ABBR = {nba: bref for bref, nba in TEAM_ABBR_TO_NBA.items()}

SEASON_MONTHS = [
    "october", "november", "december", "january", "february",
    "march", "april", "may", "june",
]


def _get(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    # requests guesses the response encoding from HTTP headers when the
    # Content-Type doesn't pin one down explicitly; confirmed against a
    # real BR page that this guesses ISO-8859-1 while the actual content
    # is UTF-8 (resp.apparent_encoding, content-sniffed, correctly said
    # utf-8) -- without this, non-ASCII player names come back mangled
    # ("Alperen ÅengÃ¼n" instead of "Alperen Şengün").
    resp.encoding = "utf-8"
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.text


def _to_bref_abbr(nba_abbr: str) -> str:
    return NBA_TO_BREF_ABBR.get(nba_abbr, nba_abbr)


def _bref_game_id(home_team_nba_abbr: str, game_date: str) -> str:
    """BR's own box score id: YYYYMMDD0<home team's bref abbreviation>.
    game_date is 'YYYY-MM-DD' (this project's convention everywhere else)."""
    return f"{game_date.replace('-', '')}0{_to_bref_abbr(home_team_nba_abbr)}"


def _parse_minutes(min_str) -> float | None:
    """BR's MP is 'MM:SS' string, same convention as nba_api's MIN."""
    if not min_str or pd.isna(min_str):
        return None
    if ":" in str(min_str):
        m, s = str(min_str).split(":")
        return int(m) + int(s) / 60
    try:
        return float(min_str)
    except (ValueError, TypeError):
        return None


def _num(val):
    """pandas/numpy scalar types (numpy.int64, numpy.float64, ...) aren't
    understood by sqlite3's default type adapter -- it silently stores
    them as a BLOB (via the buffer protocol) instead of an INTEGER/REAL,
    with no error. Caught this via a real DB-write smoke test, not
    assumed: .item() converts a numpy scalar to the equivalent native
    Python type; plain Python values pass through unchanged."""
    if pd.isna(val):
        return None
    return val.item() if hasattr(val, "item") else val


def fetch_month_schedule(season_end_year: int, month: str) -> pd.DataFrame:
    """One month's games, e.g. season_end_year=2026, month='october'.

    Returns empty DataFrame (not an error) if that month has no page,
    since not every season plays in every listed month.
    """
    url = f"{BASE_URL}/leagues/NBA_{season_end_year}_games-{month}.html"
    try:
        html = _get(url)
    except requests.HTTPError:
        return pd.DataFrame()
    tables = pd.read_html(io.StringIO(html), attrs={"id": "schedule"})
    return tables[0] if tables else pd.DataFrame()


def fetch_full_season_schedule(season_end_year: int) -> pd.DataFrame:
    """Concatenate every month's schedule for the season, e.g. 2026 = 2025-26."""
    frames = [fetch_month_schedule(season_end_year, m) for m in SEASON_MONTHS]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_team_game_log(team_abbr_bref: str, season_end_year: int) -> pd.DataFrame:
    """Team's full game log (basic stats) for the season."""
    url = f"{BASE_URL}/teams/{team_abbr_bref}/{season_end_year}/gamelog/"
    html = _get(url)
    tables = pd.read_html(io.StringIO(html), attrs={"id": "tgl_basic"})
    return tables[0] if tables else pd.DataFrame()


def _normalize_abbr(bref_abbr: str) -> str:
    return TEAM_ABBR_TO_NBA.get(bref_abbr, bref_abbr)


def persist_season_schedule(season_end_year: int, season_label: str, db_path=None):
    """Write games rows from the BR schedule, matched to existing rows by
    (date, home_team, away_team) rather than inserting a new game_id --
    BR doesn't share nba.com's id scheme, so this only fills in/confirms
    games that were already inserted by another source. Run nba_api or
    balldontlie ingestion first.
    """
    conn = get_connection(db_path) if db_path else get_connection()
    schedule = fetch_full_season_schedule(season_end_year)

    matched, unmatched = 0, 0
    for _, row in schedule.iterrows():
        game_date = pd.to_datetime(row["Date"]).date().isoformat()
        home_team = _normalize_abbr(row["Home/Neutral"])
        away_team = _normalize_abbr(row["Visitor/Neutral"])

        existing = conn.execute(
            """SELECT game_id FROM games
               WHERE game_date = ? AND home_team = ? AND away_team = ?""",
            (game_date, home_team, away_team),
        ).fetchone()

        if existing:
            matched += 1
            game_id = existing["game_id"]
            conn.execute(
                """INSERT INTO team_game_stats
                   (game_id, team, source, is_home, points)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(game_id, team, source) DO NOTHING""",
                (game_id, home_team, SOURCE_NAME, row.get("PTS.1")),
            )
            conn.execute(
                """INSERT INTO team_game_stats
                   (game_id, team, source, is_home, points)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(game_id, team, source) DO NOTHING""",
                (game_id, away_team, SOURCE_NAME, row.get("PTS")),
            )
        else:
            # No matching game from another source yet -- logged, not
            # inserted, since we have no game_id to assign it here.
            unmatched += 1

    conn.commit()
    conn.close()
    return {"matched": matched, "unmatched": unmatched}


def _read_stat_table(table) -> pd.DataFrame:
    """Parse one basic/advanced box score table (a bs4 Tag), flattening
    the two-row header (e.g. ('Basic Box Score Stats', 'PTS') -> 'PTS')
    and dropping the 'Reserves' divider row and 'Team Totals' summary
    row -- both are real <tr> rows in the HTML but not players. Verified
    column names (MP/FG/FGA/.../+/- for basic, TS%/.../ORtg/DRtg/BPM for
    advanced) against a real live box score page.

    A DNP player's stat cells hold text ('Did Not Play', 'Did Not Dress',
    'Not With Team', ...) instead of numbers -- confirmed against a real
    live run, where this broke box score persistence for 87/100 games.
    pd.read_html can't force a column to numeric when ANY cell in it is
    text, so it silently keeps the WHOLE column as strings (even the
    genuinely numeric cells, as their string repr) -- then .sum() on that
    column does STRING CONCATENATION, not addition (verified locally:
    ['22','13','Did Not Play'].sum() -> '2213Did Not Play', matching the
    exact garbled values seen in the live log). Coercing every stat
    column to numeric here (DNP text -> NaN) fixes both the per-player
    write path and the team-total sum in one place; MP and the name
    column are left alone since they're legitimately non-numeric."""
    df = pd.read_html(io.StringIO(str(table)))[0]
    df.columns = df.columns.get_level_values(-1)
    name_col = df.columns[0]  # 'Starters'
    df = df[~df[name_col].isin(["Reserves", "Team Totals"])]
    df = df.dropna(subset=[name_col]).reset_index(drop=True)
    for col in df.columns:
        if col in (name_col, "MP"):
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _extract_player_rows(table) -> list[dict]:
    """Ordered list of real player rows from one basic box score table.
    BR marks each actual player's name cell with a data-append-csv
    attribute (their site's own stable per-player slug, e.g.
    'duranke01') -- confirmed against a real live page; the repeated
    header row and the 'Reserves' divider row don't have it. Row order
    here matches _read_stat_table()'s output after its own filtering, so
    the two get zipped together by position in _persist_team_and_players."""
    rows = []
    is_starter = True
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        first = cells[0]
        if first.get_text(strip=True) == "Reserves":
            is_starter = False
            continue
        player_id = first.get("data-append-csv")
        if not player_id:
            continue  # header repeat row, or anything else that isn't a player
        rows.append({
            "player_id": player_id,
            "player_name": first.get_text(strip=True),
            "is_starter": is_starter,
        })
    return rows


def _extract_officials(soup) -> list[str]:
    """Officials are listed as plain text near the bottom of the page
    ('Officials: Pat Fraher, Nate Green, ...' with each name linked) --
    confirmed against a real live page: the label is a <strong> tag and
    the name <a> tags are its siblings under a shared parent, not its
    descendants. Empty list (not an error) if the layout doesn't have it
    -- officials are display/analysis-only, never required downstream."""
    label = soup.find(string=re.compile("Officials", re.IGNORECASE))
    if not label:
        return []
    strong = label.find_parent()
    container = strong.find_parent() if strong else None
    if not container:
        return []
    return [a.get_text(strip=True) for a in container.find_all("a") if a.get_text(strip=True)]


def fetch_game_boxscore(bref_game_id: str) -> dict:
    """Return {'<bref team abbr>': {'basic': df, 'advanced': df,
    'players': [...]}, 'officials': [...]} for one BR box score page.
    Team keys are BR's OWN abbreviations (as they appear in the page's
    table ids), not nba.com's -- callers map back via TEAM_ABBR_TO_NBA."""
    url = f"{BASE_URL}/boxscores/{bref_game_id}.html"
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")

    team_ids = sorted({
        m.group(1) for t in soup.find_all("table")
        for m in [re.match(r"box-(\w+)-game-basic", t.get("id") or "")]
        if m
    })
    if len(team_ids) != 2:
        raise RuntimeError(
            f"Expected 2 teams' basic box score tables for {bref_game_id}, found {team_ids}")

    result = {}
    for abbr in team_ids:
        basic_table = soup.find("table", id=f"box-{abbr}-game-basic")
        advanced_table = soup.find("table", id=f"box-{abbr}-game-advanced")
        if basic_table is None or advanced_table is None:
            raise RuntimeError(f"Missing basic/advanced box score table for {abbr} in {bref_game_id}")
        result[abbr] = {
            "basic": _read_stat_table(basic_table),
            "advanced": _read_stat_table(advanced_table),
            "players": _extract_player_rows(basic_table),
        }

    result["officials"] = _extract_officials(soup)
    return result


_BASIC_TOTAL_COLS = {
    "fgm": "FG", "fga": "FGA", "fg3m": "3P", "fg3a": "3PA", "ftm": "FT", "fta": "FTA",
    "oreb": "ORB", "dreb": "DRB", "reb": "TRB", "ast": "AST", "stl": "STL",
    "blk": "BLK", "tov": "TOV", "pf": "PF", "pts": "PTS",
}


def _persist_team_and_players(conn, game_id: str, team_abbr: str, is_home: int, team_box: dict) -> dict:
    """Write player_game_stats (basic+advanced merged) for one team, plus
    that team's basic team_game_stats row, and return the team's summed
    basic totals (used by _apply_pace_and_ratings afterward, once both
    teams' totals are known)."""
    basic, advanced, players = team_box["basic"], team_box["advanced"], team_box["players"]
    if len(basic) != len(players) or len(advanced) != len(players):
        raise RuntimeError(
            f"Row count mismatch for {game_id}/{team_abbr}: basic={len(basic)} "
            f"advanced={len(advanced)} player_rows={len(players)} -- BR's table "
            f"structure may have changed, refusing to silently misalign stats to players.")

    for i, meta in enumerate(players):
        b, a = basic.iloc[i], advanced.iloc[i]
        minutes = _parse_minutes(b.get("MP"))
        conn.execute(
            """INSERT INTO player_game_stats
               (game_id, player_id, player_name, team, source, is_home,
                is_starter, status, minutes, points, fgm, fga, fg3m, fg3a,
                ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf, plus_minus)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, player_id, source) DO NOTHING""",
            (
                game_id, meta["player_id"], meta["player_name"], team_abbr,
                SOURCE_NAME, is_home, int(meta["is_starter"]),
                "active" if minutes is not None else "dnp", minutes,
                _num(b.get("PTS")), _num(b.get("FG")), _num(b.get("FGA")),
                _num(b.get("3P")), _num(b.get("3PA")), _num(b.get("FT")), _num(b.get("FTA")),
                _num(b.get("ORB")), _num(b.get("DRB")), _num(b.get("TRB")), _num(b.get("AST")),
                _num(b.get("STL")), _num(b.get("BLK")), _num(b.get("TOV")), _num(b.get("PF")),
                _num(b.get("+/-")),
            ),
        )

    totals = {key: float(basic[col].sum()) if col in basic.columns else 0.0
              for key, col in _BASIC_TOTAL_COLS.items()}

    # A live run showed team point totals in the sextillions for ~13% of
    # games -- the exact digit-concatenation signature of the DNP/string
    # bug this module already fixed once, but re-fetching those SAME
    # games afterward parsed cleanly with the identical code, ruling out
    # a parsing bug. That points to a transient/flaky response under
    # sustained scraping load (100 games x ~1 request each, 2s apart)
    # rather than something fixable in the parsing logic itself. Refuse
    # to write an implausible total rather than silently corrupt the DB
    # -- real NBA team totals are never outside this range -- so a flaky
    # fetch gets skipped-and-retried (via persist_season_boxscores's
    # already-has-data check on a later run) instead of poisoning it.
    if not (40 <= totals["pts"] <= 260):
        raise RuntimeError(
            f"Implausible team points total for {game_id}/{team_abbr}: {totals['pts']} -- "
            f"likely a flaky/corrupted fetch, not a real game result. Refusing to persist it.")

    conn.execute(
        """INSERT INTO team_game_stats
           (game_id, team, source, is_home, points, fgm, fga, fg3m, fg3a,
            ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(game_id, team, source) DO UPDATE SET
               points=excluded.points, fgm=excluded.fgm, fga=excluded.fga,
               fg3m=excluded.fg3m, fg3a=excluded.fg3a, ftm=excluded.ftm,
               fta=excluded.fta, oreb=excluded.oreb, dreb=excluded.dreb,
               reb=excluded.reb, ast=excluded.ast, stl=excluded.stl,
               blk=excluded.blk, tov=excluded.tov, pf=excluded.pf""",
        (
            game_id, team_abbr, SOURCE_NAME, is_home,
            totals["pts"], totals["fgm"], totals["fga"], totals["fg3m"], totals["fg3a"],
            totals["ftm"], totals["fta"], totals["oreb"], totals["dreb"], totals["reb"],
            totals["ast"], totals["stl"], totals["blk"], totals["tov"], totals["pf"],
        ),
    )
    return totals


def _apply_pace_and_ratings(conn, game_id: str, home_team: str, away_team: str, team_totals: dict):
    """Team-level possessions/pace/ratings aren't printed anywhere on a BR
    box score page (only per-player advanced stats are) -- computed here
    from each team's basic-box-score totals using basketball-reference's
    own published pace/possession formula:
        Poss = 0.5 * ((TmFGA + 0.4*TmFTA - 1.07*(TmORB/(TmORB+OppDRB))*(TmFGA-TmFGM) + TmTOV)
                     + (OppFGA + 0.4*OppFTA - 1.07*(OppORB/(OppORB+TmDRB))*(OppFGA-OppFGM) + OppTOV))
    This formula itself was NOT re-verified against a live page in this
    session (unlike everything else in this module) -- it's from BR's
    published glossary, not something printed on the box score page
    itself. Treat with a little more skepticism than the directly-scraped
    stats; spot-check derived off_rating/pace against a few known games.

    'pace' here is the raw estimated possession count for the game, not
    normalized to a 48-minute pace-per-48 figure (that needs total team
    minutes played, which isn't collected here) -- good enough as a
    same-scale relative signal across games, not a literal NBA.com PACE
    stat equivalent.
    """
    home, away = team_totals[home_team], team_totals[away_team]

    poss = 0.5 * (
        (home["fga"] + 0.4 * home["fta"]
         - 1.07 * (home["oreb"] / (home["oreb"] + away["dreb"])) * (home["fga"] - home["fgm"])
         + home["tov"])
        + (away["fga"] + 0.4 * away["fta"]
           - 1.07 * (away["oreb"] / (away["oreb"] + home["dreb"])) * (away["fga"] - away["fgm"])
           + away["tov"])
    ) if (home["oreb"] + away["dreb"]) and (away["oreb"] + home["dreb"]) else None

    for team, opp in ((home_team, away_team), (away_team, home_team)):
        off_rtg = 100 * team_totals[team]["pts"] / poss if poss else None
        def_rtg = 100 * team_totals[opp]["pts"] / poss if poss else None
        net_rtg = (off_rtg - def_rtg) if off_rtg is not None and def_rtg is not None else None
        conn.execute(
            """UPDATE team_game_stats SET possessions = ?, off_rating = ?,
                   def_rating = ?, net_rating = ?, pace = ?
               WHERE game_id = ? AND team = ? AND source = ?""",
            (poss, off_rtg, def_rtg, net_rtg, poss, game_id, team, SOURCE_NAME),
        )


def persist_game_boxscore(game_id: str, home_team: str, away_team: str,
                          game_date: str, db_path=None):
    """Fetch + write one game's player box scores (basic+advanced merged),
    computed team-level pace/possessions/ratings, and officials -- the
    bref equivalent of nba_api_source._persist_game_detail(). home_team/
    away_team/game_date use this project's convention (nba.com
    abbreviations, 'YYYY-MM-DD') everywhere except the BR fetch itself."""
    bref_id = _bref_game_id(home_team, game_date)
    home_bref, away_bref = _to_bref_abbr(home_team), _to_bref_abbr(away_team)

    box = fetch_game_boxscore(bref_id)
    if home_bref not in box or away_bref not in box:
        raise RuntimeError(
            f"Box score for {bref_id} didn't include expected teams "
            f"{home_bref}/{away_bref} -- found {[k for k in box if k != 'officials']}")

    conn = get_connection(db_path) if db_path else get_connection()
    try:
        team_totals = {
            home_team: _persist_team_and_players(conn, game_id, home_team, 1, box[home_bref]),
            away_team: _persist_team_and_players(conn, game_id, away_team, 0, box[away_bref]),
        }
        _apply_pace_and_ratings(conn, game_id, home_team, away_team, team_totals)
        for name in box["officials"]:
            conn.execute(
                """INSERT INTO game_officials (game_id, official_name)
                   VALUES (?, ?) ON CONFLICT(game_id, official_name) DO NOTHING""",
                (game_id, name),
            )
        conn.commit()
    finally:
        conn.close()


def persist_season_boxscores(season: str, db_path=None):
    """Iterate every completed game already in the games table (inserted
    by balldontlie, the primary game/schedule discoverer now that nba_api
    is unreachable from GitHub Actions -- see nba_api_source.py's module
    docstring) and fetch+persist bref's richer per-game box score /
    advanced-stats / officials data for any game that doesn't already
    have it. The bref equivalent of nba_api_source.persist_season()'s
    per-game detail loop."""
    conn = get_connection(db_path) if db_path else get_connection()
    games = conn.execute(
        """SELECT game_id, game_date, home_team, away_team FROM games
           WHERE season = ? AND status = 'final'""",
        (season,),
    ).fetchall()
    conn.close()

    fetched, skipped, failed = 0, 0, 0
    for g in games:
        check_conn = get_connection(db_path) if db_path else get_connection()
        already = check_conn.execute(
            "SELECT 1 FROM player_game_stats WHERE game_id = ? AND source = ? LIMIT 1",
            (g["game_id"], SOURCE_NAME),
        ).fetchone()
        check_conn.close()
        if already:
            skipped += 1
            continue
        try:
            persist_game_boxscore(g["game_id"], g["home_team"], g["away_team"],
                                  g["game_date"], db_path=db_path)
            fetched += 1
        except Exception as exc:
            log.warning("bref box score fetch failed for %s (%s @ %s, %s): %s",
                       g["game_id"], g["away_team"], g["home_team"], g["game_date"], exc)
            failed += 1

    log.info("bref box scores: %d fetched, %d already had data, %d failed", fetched, skipped, failed)
    return {"fetched": fetched, "skipped": skipped, "failed": failed}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _extract_player_ids(table) -> list[dict]:
    """Ordered list of {'player_id','player_name'} for any BR table whose
    player rows carry a data-append-csv attribute -- the same site-wide
    convention confirmed on box score pages (see _extract_player_rows).

    Unlike a box score table (where the player name IS the first cell),
    the roster table's first cell is the 'No.' (jersey number) column --
    confirmed against a real live page, where this caused 0 players to
    be found for all 30 teams despite real rows existing. Searches every
    cell in the row rather than assuming a fixed position."""
    out = []
    for tr in table.find_all("tr"):
        for cell in tr.find_all(["th", "td"]):
            player_id = cell.get("data-append-csv")
            if player_id:
                out.append({"player_id": player_id, "player_name": cell.get_text(strip=True)})
                break
    return out


_POSITION_BUCKET = {"PG": "PG", "SG": "SG", "SF": "SF", "PF": "PF", "C": "C",
                    "G": "SG", "F": "SF"}  # coarse fallback if BR gives a plain guard/forward


def _bref_bucket_position(raw_pos) -> str | None:
    """BR roster 'Pos' is usually already PG/SG/SF/PF/C, sometimes a
    combo like 'SG-SF' (take the first) or a plain 'G'/'F' (coarse
    fallback)."""
    if not raw_pos or pd.isna(raw_pos):
        return None
    first = str(raw_pos).split("-")[0].strip().upper()
    return _POSITION_BUCKET.get(first)


def _height_to_inches(height_str) -> float | None:
    """BR's Ht column is 'FT-IN' e.g. '6-9' -> 81.0, same format nba_api uses."""
    if not height_str or pd.isna(height_str) or "-" not in str(height_str):
        return None
    try:
        ft, inch = str(height_str).split("-")
        return int(ft) * 12 + int(inch)
    except (ValueError, TypeError):
        return None


def fetch_team_roster(bref_abbr: str, season_end_year: int) -> list[dict]:
    """Current-season roster (position, height) for one team.

    NOT diagnosed against a live page in this session, unlike the box
    score scraper above -- BR's roster table (id='roster', with 'Pos'
    and 'Ht' columns) is a long-stable, widely-documented page, but
    spot-check the first real run's row counts/values before trusting
    it fully, the same discipline applied everywhere else in this module.
    """
    url = f"{BASE_URL}/teams/{bref_abbr}/{season_end_year}.html"
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="roster")
    if table is None:
        return []

    df = pd.read_html(io.StringIO(str(table)))[0]
    player_rows = _extract_player_ids(table)
    if len(df) != len(player_rows):
        raise RuntimeError(
            f"Roster row count mismatch for {bref_abbr}/{season_end_year}: "
            f"table={len(df)} player_rows={len(player_rows)} -- BR's roster table "
            f"structure may differ from what was assumed, refusing to silently misalign.")

    return [
        {
            "player_id": meta["player_id"],
            "player_name": meta["player_name"],
            "position": _bref_bucket_position(df.iloc[i].get("Pos")),
            "height_inches": _height_to_inches(df.iloc[i].get("Ht")),
        }
        for i, meta in enumerate(player_rows)
    ]


def persist_season_rosters(season_end_year: int, db_path=None):
    """Write position/height into the players reference table from every
    team's current roster page -- the bref equivalent of
    nba_api_source.persist_player_bio(). Refresh periodically (not every
    hourly run); positions/heights don't change mid-season."""
    from src.ingestion.nba_teams import NBA_TEAMS

    conn = get_connection(db_path) if db_path else get_connection()
    now = _now_iso()
    written, failed_teams = 0, 0
    for nba_abbr, _full, _nick in NBA_TEAMS:
        try:
            roster = fetch_team_roster(_to_bref_abbr(nba_abbr), season_end_year)
        except Exception as exc:
            log.warning("bref roster fetch failed for %s: %s", nba_abbr, exc)
            failed_teams += 1
            continue
        for p in roster:
            conn.execute(
                """INSERT INTO players
                   (player_id, player_name, position, height_inches, current_team, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(player_id) DO UPDATE SET
                       player_name = excluded.player_name,
                       position = excluded.position,
                       height_inches = excluded.height_inches,
                       current_team = excluded.current_team,
                       updated_at = excluded.updated_at""",
                (p["player_id"], p["player_name"], p["position"], p["height_inches"],
                 nba_abbr, SOURCE_NAME, now),
            )
            written += 1
    conn.commit()
    conn.close()
    log.info("bref rosters: %d players written, %d/%d team fetches failed",
             written, failed_teams, len(NBA_TEAMS))
    return {"written": written, "failed_teams": failed_teams}
