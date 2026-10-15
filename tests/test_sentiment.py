"""Offline tests for the Yahoo Finance RSS headline fetcher."""

import importlib.util
import io
import sys
import types
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest


@pytest.fixture
def sentiment(monkeypatch):
    termcolor = types.ModuleType("termcolor")
    termcolor.cprint = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "termcolor", termcolor)
    spec = importlib.util.spec_from_file_location("sentiment", Path(__file__).resolve().parents[1] / "sentiment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ago(days):
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days))


def rss(*items):
    body = "".join(
        f"<item><title>{title}</title>{f'<pubDate>{date}</pubDate>' if date else ''}</item>"
        for title, date in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


def serve(monkeypatch, sentiment, feeds):
    def urlopen(req, timeout):
        feed = feeds[urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["s"][0]]
        if isinstance(feed, Exception):
            raise feed
        return io.BytesIO(feed)

    monkeypatch.setattr(sentiment.urllib.request, "urlopen", urlopen)


def titles(headlines):
    return [h["title"] for h in headlines]


def test_recent_headlines_are_newest_first_and_capped(sentiment, monkeypatch):
    days = range(sentiment.NEWS_PER_TICKER + 3)
    serve(monkeypatch, sentiment, {"AAA": rss(*[(f"Story {d}", ago(d)) for d in reversed(days)])})
    expected = [f"Story {d}" for d in days][: sentiment.NEWS_PER_TICKER]
    assert titles(sentiment.fetch_news(["AAA"])["AAA"]) == expected


def test_old_undated_and_untitled_items_are_dropped(sentiment, monkeypatch):
    naive = format_datetime((datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None))
    serve(monkeypatch, sentiment, {"AAA": rss(
        ("Fresh", ago(1)), ("Naive timezone", naive), ("Stale", ago(sentiment.NEWS_DAYS + 1)),
        ("No date", None), ("Bad date", "not a date"), ("", ago(1)),
    )})
    assert titles(sentiment.fetch_news(["AAA"])["AAA"]) == ["Fresh", "Naive timezone"]


def test_failed_ticker_is_none_and_others_still_load(sentiment, monkeypatch):
    serve(monkeypatch, sentiment, {
        "AAA": OSError("timed out"), "BBB": b"<html>not rss", "CCC": rss(("Fresh", ago(1))),
    })
    results = sentiment.fetch_news(["AAA", "BBB", "CCC"])
    assert results["AAA"] is None and results["BBB"] is None
    assert titles(results["CCC"]) == ["Fresh"]


def test_format_news_distinguishes_loaded_empty_and_failed(sentiment):
    text = sentiment.format_news({"AAA": [{"date": "2026-10-15", "title": "Fresh"}], "BBB": [], "CCC": None})
    assert "    - 2026-10-15 Fresh" in text
    assert f"BBB: No headlines in the last {sentiment.NEWS_DAYS} days" in text
    assert "CCC: Headlines unavailable" in text


def test_format_news_falls_back_when_no_ticker_has_headlines(sentiment):
    assert sentiment.format_news({"AAA": [], "BBB": None}) == "No news headlines available for portfolio tickers."
