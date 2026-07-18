"""
sentiment.py — Alternative data sources for Proteus.

Fetches news sentiment (Alpha Vantage) for portfolio tickers.
"""

import json
import urllib.request
import urllib.parse
from termcolor import cprint


ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"


# ---------------------------------------------------------------------------
# Alpha Vantage — News Sentiment
# ---------------------------------------------------------------------------


def fetch_news_sentiment(tickers, api_key):
    """
    Fetch AI-scored news sentiment for a list of tickers via Alpha Vantage.

    Uses a single batched API call (1 request for all tickers).

    Args:
        tickers: list of stock ticker strings (e.g. ["AAPL", "AMD"])
        api_key: Alpha Vantage API key (free tier: 25 requests/day)

    Returns:
        dict mapping ticker -> sentiment dict or None, e.g.:
        {
            "AAPL": {
                "article_count": 12,
                "avg_score": 0.15,
                "label_distribution": {"Bullish": 3, "Somewhat-Bullish": 5, ...},
                "top_articles": [{"title": "...", "score": 0.28, "label": "Bullish"}],
            },
            "AMD": None,  # no articles found
        }
    """
    if not api_key:
        cprint("  ⚠️ ALPHAVANTAGE_API_KEY not set, skipping news sentiment", "yellow")
        return {t: None for t in tickers}

    # Strip Yahoo Finance suffixes for the API call (e.g. LGEN.L → LGEN)
    clean_tickers = [t.split(".")[0] for t in tickers]
    ticker_map = dict(zip(clean_tickers, tickers))  # clean → original

    # Single batched request for all tickers
    params = urllib.parse.urlencode(
        {
            "function": "NEWS_SENTIMENT",
            "tickers": ",".join(clean_tickers),
            "sort": "RELEVANCE",
            "limit": 50,
            "apikey": api_key,
        }
    )
    url = f"{ALPHAVANTAGE_BASE}?{params}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        cprint(f"  ⚠️ Alpha Vantage request failed: {e}", "yellow")
        return {t: None for t in tickers}

    if "feed" not in data:
        error_msg = data.get("Note") or data.get("Information") or "Unknown error"
        cprint(f"  ⚠️ Alpha Vantage API error: {error_msg}", "yellow")
        return {t: None for t in tickers}

    # Group articles by ticker
    ticker_data = {t: {"scores": [], "labels": [], "articles": []} for t in clean_tickers}

    for article in data["feed"]:
        for ts in article.get("ticker_sentiment", []):
            tick = ts.get("ticker", "")
            if tick not in ticker_data:
                continue

            score = float(ts.get("ticker_sentiment_score", 0))
            label = ts.get("ticker_sentiment_label", "Neutral")
            relevance = float(ts.get("relevance_score", 0))

            ticker_data[tick]["scores"].append(score)
            ticker_data[tick]["labels"].append(label)
            ticker_data[tick]["articles"].append(
                {
                    "title": article.get("title", "Untitled"),
                    "score": score,
                    "label": label,
                    "relevance": relevance,
                }
            )

    # Build results keyed by original ticker names
    results = {}
    for clean, original in ticker_map.items():
        td = ticker_data.get(clean)
        if not td or not td["scores"]:
            results[original] = None
            continue

        # Label distribution
        label_dist = {}
        for lbl in td["labels"]:
            label_dist[lbl] = label_dist.get(lbl, 0) + 1

        # Top 3 articles by relevance
        top = sorted(td["articles"], key=lambda a: a["relevance"], reverse=True)[:3]

        results[original] = {
            "article_count": len(td["scores"]),
            "avg_score": round(sum(td["scores"]) / len(td["scores"]), 4),
            "label_distribution": label_dist,
            "top_articles": [
                {"title": a["title"], "score": a["score"], "label": a["label"]}
                for a in top
            ],
        }

    return results


def format_news_sentiment(results):
    """
    Format Alpha Vantage news sentiment into a readable string for the Claude prompt.

    Args:
        results: dict from fetch_news_sentiment()

    Returns:
        str — formatted summary, or fallback message if empty
    """
    lines = []
    has_data = False

    for ticker, data in results.items():
        if data is None:
            lines.append(f"  {ticker}: No news sentiment data available")
            continue

        has_data = True
        avg = data["avg_score"]
        count = data["article_count"]

        # Determine dominant label
        dist = data["label_distribution"]
        dominant = max(dist, key=dist.get) if dist else "Neutral"

        lines.append(f"  {ticker}: {avg:+.3f} avg score | {count} articles | {dominant}")
        for art in data["top_articles"]:
            lines.append(f"    - [{art['label']}] {art['title']}")

    if not has_data:
        return "No news sentiment data available for portfolio tickers."

    return "\n".join(lines)
