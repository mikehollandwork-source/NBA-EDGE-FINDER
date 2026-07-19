"""Shared NBA team name/abbreviation lookup, used by every scraped public-
sentiment/market source so they all key games the same way our nba_api-based
`team` abbreviation does (matches the pattern the MLB repo's
public_sources._NICK2ABBR / _name_abbr serves there).
"""

# (canonical abbreviation, full name, nickname)
NBA_TEAMS = [
    ("ATL", "Atlanta Hawks", "Hawks"),
    ("BOS", "Boston Celtics", "Celtics"),
    ("BKN", "Brooklyn Nets", "Nets"),
    ("CHA", "Charlotte Hornets", "Hornets"),
    ("CHI", "Chicago Bulls", "Bulls"),
    ("CLE", "Cleveland Cavaliers", "Cavaliers"),
    ("DAL", "Dallas Mavericks", "Mavericks"),
    ("DEN", "Denver Nuggets", "Nuggets"),
    ("DET", "Detroit Pistons", "Pistons"),
    ("GSW", "Golden State Warriors", "Warriors"),
    ("HOU", "Houston Rockets", "Rockets"),
    ("IND", "Indiana Pacers", "Pacers"),
    ("LAC", "LA Clippers", "Clippers"),
    ("LAL", "Los Angeles Lakers", "Lakers"),
    ("MEM", "Memphis Grizzlies", "Grizzlies"),
    ("MIA", "Miami Heat", "Heat"),
    ("MIL", "Milwaukee Bucks", "Bucks"),
    ("MIN", "Minnesota Timberwolves", "Timberwolves"),
    ("NOP", "New Orleans Pelicans", "Pelicans"),
    ("NYK", "New York Knicks", "Knicks"),
    ("OKC", "Oklahoma City Thunder", "Thunder"),
    ("ORL", "Orlando Magic", "Magic"),
    ("PHI", "Philadelphia 76ers", "76ers"),
    ("PHX", "Phoenix Suns", "Suns"),
    ("POR", "Portland Trail Blazers", "Trail Blazers"),
    ("SAC", "Sacramento Kings", "Kings"),
    ("SAS", "San Antonio Spurs", "Spurs"),
    ("TOR", "Toronto Raptors", "Raptors"),
    ("UTA", "Utah Jazz", "Jazz"),
    ("WAS", "Washington Wizards", "Wizards"),
]

ABBR_TO_FULL = {abbr: full for abbr, full, _nick in NBA_TEAMS}
FULL_TO_ABBR = {full: abbr for abbr, full, _nick in NBA_TEAMS}
VALID_ABBRS = {abbr for abbr, _full, _nick in NBA_TEAMS}

# Alternate abbreviations other sites use for the same team, normalized to ours.
ALT_ABBR = {
    "GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "WSH": "WAS",
    "PHO": "PHX", "BRK": "BKN", "CHO": "CHA", "UTAH": "UTA",
}

# nickname (lowercase) -> abbr, for text-scraping sites that label rows with
# team names/nicknames instead of codes (same technique as MLB's _NICK2ABBR).
NICK_TO_ABBR = {nick.lower(): abbr for abbr, _full, nick in NBA_TEAMS}
# "sixers" is the common spoken nickname for PHI even though the official one
# is "76ers" -- added alongside, same pattern as no other team needing this.
NICK_TO_ABBR["sixers"] = "PHI"
NICK_TO_ABBR["blazers"] = "POR"


def canon_abbr(abbr: str) -> str:
    """Normalize any site's abbreviation variant to our canonical one."""
    a = (abbr or "").strip().upper()
    return ALT_ABBR.get(a, a)


def name_to_abbr(text: str) -> str | None:
    """Canonical abbr for a token that IS a team name/nickname, else None.
    Endswith-nickname match, length-capped so prose can't hit."""
    low = text.strip().lower()
    if not 3 <= len(low) <= 30:
        return None
    for nick, ab in NICK_TO_ABBR.items():
        if low.endswith(nick):
            return ab
    return None
