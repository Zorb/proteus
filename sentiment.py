"""
sentiment.py — Alternative data sources for Proteus.

Fetches recent news headlines (Yahoo Finance RSS, no API key) for portfolio tickers.
"""

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from termcolor import cprint


YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
NEWS_DAYS = 14  # small caps can go a week or more without a headline
NEWS_PER_TICKER = 8  # the feed includes articles that only mention the ticker


# ---------------------------------------------------------------------------
# Yahoo Finance — News Headlines
# ---------------------------------------------------------------------------


def fetch_news(tickers):
    """
    Fetch recent headlines for each ticker from Yahoo Finance's RSS feed.

    Free and keyless; one request per ticker. (yfinance's .news is dead:
    Yahoo answers its endpoint with a 404 and yfinance returns [] silently.)

    Args:
        tickers: list of Yahoo Finance ticker strings (e.g. ["AAPL", "LGEN.L"])

    Returns:
        dict mapping ticker -> headlines from the last NEWS_DAYS days, newest
        first, or None if the fetch failed, e.g.:
        {
            "AAPL": [{"date": "2026-10-15", "title": "..."}],
            "AMD": [],       # no recent headlines
            "LGEN.L": None,  # request or parse failed
        }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)
    results = {}

    for ticker in tickers:
        url = YAHOO_RSS.format(ticker=urllib.parse.quote(ticker))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                root = ET.fromstring(resp.read())
        except Exception as e:
            cprint(f"  ⚠️ News fetch failed for {ticker}: {e}", "yellow")
            results[ticker] = None
            continue

        headlines = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            try:
                published = parsedate_to_datetime(item.findtext("pubDate"))
            except (TypeError, ValueError):
                continue  # missing or malformed date
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if title and published >= cutoff:
                headlines.append((published, title))

        headlines.sort(reverse=True)
        results[ticker] = [
            {"date": published.strftime("%Y-%m-%d"), "title": title}
            for published, title in headlines[:NEWS_PER_TICKER]
        ]

    return results


def format_news(results):
    """
    Format headlines into a readable string for the Claude prompt.

    Args:
        results: dict from fetch_news()

    Returns:
        str — per-ticker headline list, or fallback message if no ticker has any
    """
    if not any(results.values()):
        return "No news headlines available for portfolio tickers."

    lines = []
    for ticker, headlines in results.items():
        if headlines is None:
            lines.append(f"  {ticker}: Headlines unavailable (fetch failed)")
        elif not headlines:
            lines.append(f"  {ticker}: No headlines in the last {NEWS_DAYS} days")
        else:
            lines.append(f"  {ticker}:")
            lines.extend(f"    - {h['date']} {h['title']}" for h in headlines)

    return "\n".join(lines)
