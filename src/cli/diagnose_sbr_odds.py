"""One-off diagnostic, NOT part of the regular pipeline.

SportsbookReviewsOnline historical odds files were always documented as
a manual download (see sbr_historical_odds.py's module docstring) since
this project never had a confirmed URL for them and no live network
access to check. Now that GitHub Actions has proven real internet
access, this checks the actual site structure to find the real download
link for the current NBA season's file, rather than guessing a URL.

python -m src.cli.diagnose_sbr_odds
"""

import re

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (research script; contact via repo owner)"}
CANDIDATE_PAGES = [
    "https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba/nbaoddsarchives.htm",
    "https://www.sportsbookreviewsonline.com/scoresoddsarchives/nba-odds-archive/",
    "https://www.sportsbookreviewsonline.com/",
]


def diagnose():
    for url in CANDIDATE_PAGES:
        print(f"\nFetching {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            print(f"status={resp.status_code} bytes={len(resp.content)}")
        except requests.exceptions.RequestException as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}")
            continue
        if resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        links = soup.find_all("a", href=True)
        print(f"{len(links)} total <a> links on page")

        xlsx_links = [a for a in links if ".xlsx" in a["href"].lower()]
        print(f"{len(xlsx_links)} links containing '.xlsx'")
        for a in xlsx_links[:20]:
            print(f"  text={a.get_text(strip=True)!r} href={a['href']!r}")

        nba_links = [a for a in links if "nba" in a["href"].lower() and (".xlsx" in a["href"].lower() or "2025" in a["href"] or "2026" in a["href"])]
        print(f"{len(nba_links)} links mentioning nba + (xlsx or 2025/2026)")
        for a in nba_links[:20]:
            print(f"  text={a.get_text(strip=True)!r} href={a['href']!r}")

        # dump a broader sample of href text so an unexpected URL pattern is still visible
        print("Sample of first 30 hrefs on the page:")
        for a in links[:30]:
            print(f"  {a.get_text(strip=True)!r} -> {a['href']!r}")


if __name__ == "__main__":
    diagnose()
