"""
sentiment.py — Alternative data sources for Proteus.

Fetches prediction market data (Polymarket) and news sentiment (Alpha Vantage)
for portfolio tickers.
"""

import json
import urllib.request
import urllib.parse
from termcolor import cprint


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"


def fetch_polymarket_earnings(tickers):
    """
    Fetch earnings-related prediction markets for a list of tickers.

    Args:
        tickers: list of stock ticker strings (e.g. ["AAPL", "TSLA"])

    Returns:
        dict mapping ticker -> list of market dicts, e.g.:
        {
            "AAPL": [
                {
                    "question": "Will AAPL beat Q1 2026 earnings?",
                    "probability": 0.62,
                    "volume": 125000,
                    "end_date": "2026-04-25",
                    "active": True,
                }
            ],
            "TSLA": [],
        }
    """
    results = {}
    for ticker in tickers:
        results[ticker] = _search_earnings_markets(ticker)
    return results


def _search_earnings_markets(ticker):
    """Search Polymarket for earnings-related markets for a single ticker."""
    markets = []

    # Try multiple search terms to maximise coverage
    search_terms = [
        f"{ticker} earnings",
        f"{ticker} revenue",
    ]

    seen_ids = set()

    for term in search_terms:
        try:
            params = urllib.parse.urlencode(
                {
                    "limit": 5,
                    "active": "true",
                    "closed": "false",
                    "order": "volume",
                    "ascending": "false",
                }
            )
            url = f"{GAMMA_API_BASE}/events?{params}&title={urllib.parse.quote(term)}"

            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            if not isinstance(data, list):
                continue

            for event in data:
                event_id = event.get("id")
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)

                title = event.get("title", "")
                # Only include if the ticker actually appears in the title
                if ticker.upper() not in title.upper():
                    continue

                # Extract markets (outcomes) from the event
                event_markets = event.get("markets", [])
                for mkt in event_markets:
                    outcome_price = mkt.get("outcomePrices")
                    if outcome_price:
                        try:
                            prices = json.loads(outcome_price)
                            probability = float(prices[0]) if prices else None
                        except (json.JSONDecodeError, ValueError, IndexError):
                            probability = None
                    else:
                        probability = None

                    markets.append(
                        {
                            "question": mkt.get("question", title),
                            "probability": round(probability, 3)
                            if probability is not None
                            else None,
                            "volume": mkt.get("volume", 0),
                            "end_date": mkt.get("endDate", event.get("endDate")),
                            "active": mkt.get("active", True),
                        }
                    )

        except Exception as e:
            cprint(f"  ⚠️ Polymarket search error for '{term}': {e}", "yellow")
            continue

    return markets


def format_polymarket_data(polymarket_results):
    """
    Format Polymarket results into a readable string for the Claude prompt.

    Args:
        polymarket_results: dict from fetch_polymarket_earnings()

    Returns:
        str — formatted summary, or "No Polymarket data available" if empty
    """
    lines = []
    has_data = False

    for ticker, markets in polymarket_results.items():
        if not markets:
            lines.append(f"  {ticker}: No earnings markets found")
            continue

        has_data = True
        lines.append(f"  {ticker}:")
        for mkt in markets:
            prob_str = (
                f"{mkt['probability'] * 100:.1f}%"
                if mkt["probability"] is not None
                else "N/A"
            )
            vol_str = f"${mkt['volume']:,.0f}" if mkt["volume"] else "N/A"
            lines.append(f"    - {mkt['question']}")
            lines.append(
                f"      Probability: {prob_str} | Volume: {vol_str} | Ends: {mkt['end_date'] or 'N/A'}"
            )

    if not has_data:
        return "No Polymarket earnings data available for portfolio tickers."

    return "\n".join(lines)


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
