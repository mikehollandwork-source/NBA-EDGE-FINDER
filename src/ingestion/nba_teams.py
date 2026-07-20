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


# Arena coordinates (lat, lon), for the travel-distance feature. Static --
# arenas don't move mid-season; a team relocating/renaming (rare) would
# need a one-line update here.
ARENA_COORDS = {
    "ATL": (33.7573, -84.3963), "BOS": (42.3662, -71.0621),
    "BKN": (40.6826, -73.9754), "CHA": (35.2251, -80.8392),
    "CHI": (41.8807, -87.6742), "CLE": (41.4965, -81.6882),
    "DAL": (32.7905, -96.8103), "DEN": (39.7487, -105.0077),
    "DET": (42.3410, -83.0550), "GSW": (37.7680, -122.3877),
    "HOU": (29.7508, -95.3621), "IND": (39.7640, -86.1555),
    "LAC": (34.0430, -118.2673), "LAL": (34.0430, -118.2673),
    "MEM": (35.1382, -90.0505), "MIA": (25.7814, -80.1870),
    "MIL": (43.0451, -87.9172), "MIN": (44.9795, -93.2761),
    "NOP": (29.9490, -90.0821), "NYK": (40.7505, -73.9934),
    "OKC": (35.4634, -97.5151), "ORL": (28.5392, -81.3839),
    "PHI": (39.9012, -75.1720), "PHX": (33.4457, -112.0712),
    "POR": (45.5316, -122.6668), "SAC": (38.6491, -121.5180),
    "SAS": (29.4269, -98.4375), "TOR": (43.6435, -79.3791),
    "UTA": (40.7683, -111.9011), "WAS": (38.8981, -77.0209),
}


def travel_distance_miles(from_abbr: str, to_abbr: str) -> float | None:
    """Great-circle distance between two teams' arenas, in miles."""
    import math

    a, b = ARENA_COORDS.get(from_abbr), ARENA_COORDS.get(to_abbr)
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))  # Earth radius in miles
