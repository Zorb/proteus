"""Offline integration fixtures. Never import config or load a credential file."""

import ast
import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def agent_modules(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = types.ModuleType("config")
    values = {
        "IBKR_FLEX_TOKEN": "", "IBKR_FLEX_QUERY_ID": "", "ANTHROPIC_KEY": "",
        "MODEL_NAME": "offline-test",
        "ACTION_TP_LADDER": [(25., 25.), (50., 50.)],
        "ACTION_CL_LADDER": [(-8., 50.), (-15., 100.)],
        "ACTION_RED_TP_CAP": 75., "ACTION_RED_CL_CAP": 100.,
        "ALERT_MIN_POSITION_PCT": 5., "NOTIFICATION_METHOD": "email",
        "SMTP_USER": "test", "SMTP_PASSWORD": "test", "EMAIL_TO": "test@example.invalid",
        "EMAIL_FROM": "test@example.invalid", "SMTP_HOST": "example.invalid", "SMTP_PORT": 587,
        "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test",
    }
    config.__dict__.update(values)
    # Read just the prompt literal from source; config.py's dotenv call is never executed.
    for node in ast.parse((root / "config.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RISK_PROMPT" for t in node.targets):
            config.RISK_PROMPT = ast.literal_eval(node.value)
    monkeypatch.setitem(sys.modules, "config", config)
    for name in ("schedule", "yfinance", "anthropic", "telegram", "termcolor"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["telegram"].Bot = None
    sys.modules["termcolor"].cprint = lambda *a, **kw: None
    sys.modules["termcolor"].colored = lambda value, *a, **kw: value
    loaded = []
    for name in ("portfolio_sync", "sentiment", "stock_risk_agent"):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    sync, sentiment, main = loaded
    monkeypatch.setattr(main.time, "sleep", lambda *a: None)

    def no_network(*a, **kw):
        raise AssertionError("Network calls are forbidden in these tests")

    monkeypatch.setattr(sync.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(main.smtplib, "SMTP", no_network)
    monkeypatch.setattr(sentiment, "fetch_news", lambda tickers: {t: [] for t in tickers})
    return types.SimpleNamespace(main=main, sync=sync, config=config)


@pytest.fixture
def history_clock(agent_modules, monkeypatch):
    # UK Monday is still Sunday UTC: exercise the midnight boundary reliably.
    now = pd.Timestamp("2026-09-13 23:30:00", tz="UTC")
    clock_pd = types.ModuleType("pandas")
    clock_pd.__dict__.update(vars(pd))
    clock_pd.Timestamp = types.SimpleNamespace(
        now=lambda tz=None: now.tz_convert(tz) if tz is not None
        else now.tz_convert("Europe/London").tz_localize(None)
    )
    monkeypatch.setattr(agent_modules.main, "pd", clock_pd)
    return now


@pytest.fixture
def analysis_case(agent_modules, monkeypatch):
    modules = agent_modules
    agent = modules.main.StockRiskAgent()
    idx = pd.bdate_range(end="2026-09-11", periods=260)
    rng = np.random.default_rng(24)
    spy = pd.Series(rng.normal(0.0005, 0.01, len(idx)), index=idx)
    history = pd.DataFrame({"Close": 100 * (1 + spy).cumprod(), "Volume": 1_000_000.}, index=idx)
    frame = pd.DataFrame([{"Ticker": "AAA", "Position_Size": 10., "Avg_Price": 100.}])
    data = {
        "ticker": "AAA", "currency": "USD", "current_price": 110.,
        "hist_1y": history, "hist_3y": history, "hist_5y": history,
        "recent_closes": [], "pe_ratio": 20., "market_cap": 1_000_000.,
        "bid": 109.99, "ask": 110.01, "avg_volume": 1_000_000., "sector": "Technology",
        "balance_sheet": pd.DataFrame({"latest": [100., 500., 100.]}, index=["Total Debt", "Current Assets", "Current Liabilities"]),
        "income_stmt": pd.DataFrame({"latest": [1000., 10.]}, index=["EBITDA", "Interest Expense"]),
        "vol_30d": 1_000_000., "vol_90d": 1_000_000., "data_quality_issues": [],
    }
    reports, prompts, snapshots = [], [], []
    case = types.SimpleNamespace(agent=agent, modules=modules, frame=frame, data=data,
                                 reports=reports, prompts=prompts, snapshots=snapshots, history=history, spy=spy)
    monkeypatch.setattr(modules.main.pd, "read_csv", lambda *a, **kw: case.frame.copy())
    monkeypatch.setattr(pd.DataFrame, "to_csv", lambda df, *a, **kw: snapshots.append(df.copy()))
    monkeypatch.setattr(agent, "fetch_data", lambda ticker: dict(case.data, ticker=ticker) if case.data else None)
    monkeypatch.setattr(agent, "fetch_benchmarks", lambda: {"spy_returns": spy, "vix_close": pd.Series(30., index=idx),
                                                         "tnx_changes": spy, "risk_free_rate": .04, "tnx_available": True})

    def create(**kw):
        prompts.append(kw["messages"][0]["content"])
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Synthetic analysis")],
                                     stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=0, output_tokens=0))

    agent.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(agent, "send_email", lambda text, summary=None: reports.append((text, summary)))
    return case
