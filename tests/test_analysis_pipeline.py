import types

import pandas as pd
import pytest


def test_valid_pipeline_uses_market_value_and_emits_report(analysis_case):
    case = analysis_case
    summary = case.agent.analyze_portfolio()
    assert summary["total_value"] == 1100
    assert summary["currency"] == "USD"
    assert summary["stocks"][0]["position_pct"] == 100
    assert len(case.reports) == len(case.prompts) == 1
    assert "No previous-run comparison" in case.prompts[0]


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf")])
def test_unpriced_holding_cannot_create_a_green_portfolio(analysis_case, price):
    case = analysis_case
    case.data["current_price"] = price
    case.frame.loc[0, "Avg_Price"] = float("nan")
    with pytest.raises(case.modules.main.AnalysisError, match="valuation unavailable"):
        case.agent.job(reraise=True)
    assert not case.prompts
    assert len(case.reports) == 1
    assert case.reports[0][1] is None
    assert "ANALYSIS UNAVAILABLE" in case.reports[0][0]


def test_entry_cost_does_not_replace_missing_market_price(analysis_case):
    case = analysis_case
    case.data["current_price"] = None
    assert case.frame.iloc[0]["Avg_Price"] == 100
    with pytest.raises(case.modules.main.AnalysisError, match="entry cost"):
        case.agent.analyze_portfolio()


def test_failed_ticker_is_not_silently_removed(analysis_case, monkeypatch):
    case = analysis_case
    case.frame.loc[1] = ["BBB", 10, 100]
    monkeypatch.setattr(case.agent, "fetch_data", lambda ticker: dict(case.data) if ticker == "AAA" else None)
    with pytest.raises(case.modules.main.AnalysisError, match="BBB"):
        case.agent.analyze_portfolio()
    assert not case.prompts and not case.reports


def test_non_usd_market_quote_fails_even_for_legacy_csv(analysis_case):
    case = analysis_case
    case.data["currency"] = "GBP"
    with pytest.raises(case.modules.main.AnalysisError, match="currency must be USD"):
        case.agent.analyze_portfolio()


def test_duplicate_csv_rows_do_not_split_weights(analysis_case):
    case = analysis_case
    case.frame = pd.DataFrame([{"Ticker": "AAA", "Position_Size": 60, "Avg_Price": 100},
                               {"Ticker": "AAA", "Position_Size": 10, "Avg_Price": 100},
                               {"Ticker": "BBB", "Position_Size": 30, "Avg_Price": 100}])
    summary = case.agent.analyze_portfolio()
    assert len(summary["stocks"]) == 2
    assert [s["position_pct"] for s in summary["stocks"]] == pytest.approx([70, 30])
    assert summary["portfolio_return_coverage"]["data_available"] is True


def test_empty_broker_snapshot_replaces_old_holdings(analysis_case, monkeypatch):
    case = analysis_case
    case.modules.config.IBKR_FLEX_TOKEN = case.modules.config.IBKR_FLEX_QUERY_ID = "test"
    empty = case.frame.iloc[:0]
    monkeypatch.setattr(case.modules.sync, "fetch_ibkr_portfolio", lambda: empty)
    monkeypatch.setattr(case.modules.main.pd, "read_csv", lambda *a, **kw: pytest.fail("Must not read old holdings"))
    summary = case.agent.analyze_portfolio()
    assert summary["stocks"] == []
    assert summary["portfolio_composite_score"] is None
    assert case.snapshots[0].empty
    assert len(case.reports) == 1 and not case.prompts
    assert "0/100" not in case.agent.format_html_report(case.reports[0][0], summary)


def test_bad_broker_holdings_do_not_overwrite_snapshot(analysis_case, monkeypatch):
    case = analysis_case
    case.modules.config.IBKR_FLEX_TOKEN = case.modules.config.IBKR_FLEX_QUERY_ID = "test"
    bad = case.frame.copy()
    bad.loc[0, "Position_Size"] = -10
    monkeypatch.setattr(case.modules.sync, "fetch_ibkr_portfolio", lambda: bad)
    with pytest.raises(case.modules.sync.PortfolioValidationError):
        case.agent.analyze_portfolio()
    assert case.snapshots == []


def test_broker_outage_warns_on_csv_fallback(analysis_case, monkeypatch):
    case = analysis_case
    case.modules.config.IBKR_FLEX_TOKEN = case.modules.config.IBKR_FLEX_QUERY_ID = "test"
    monkeypatch.setattr(case.modules.sync, "fetch_ibkr_portfolio", lambda: None)
    summary = case.agent.analyze_portfolio()
    assert summary["data_source"] == "csv_fallback"
    assert summary["data_complete"] is False
    assert "POSITION DATA WARNING" in case.reports[0][0]


def test_ai_failure_delivers_one_quantitative_report_and_fails_job(analysis_case, monkeypatch):
    case = analysis_case

    def fail(**kw):
        raise RuntimeError("AI offline")

    monkeypatch.setattr(case.agent.client.messages, "create", fail)
    with pytest.raises(case.modules.main.AnalysisError, match="quantitative fallback report delivered"):
        case.agent.job(reraise=True)
    assert len(case.reports) == 1
    assert "AI INTERPRETATION UNAVAILABLE" in case.reports[0][0]
    assert case.reports[0][1]["total_value"] == 1100


@pytest.mark.parametrize("text,stop_reason", [("", "end_turn"), ("Partial", "max_tokens")])
def test_empty_or_truncated_ai_response_uses_fallback(analysis_case, monkeypatch, text, stop_reason):
    case = analysis_case
    message = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)
    monkeypatch.setattr(case.agent.client.messages, "create", lambda **kw: message)
    with pytest.raises(case.modules.main.AnalysisError):
        case.agent.job(reraise=True)
    assert len(case.reports) == 1
    assert "AI INTERPRETATION UNAVAILABLE" in case.reports[0][0]


def test_missing_ai_configuration_does_not_prevent_numeric_analysis(analysis_case, monkeypatch):
    case = analysis_case
    case.agent.client = None

    def fail():
        raise RuntimeError("AI not configured")

    monkeypatch.setattr(case.agent, "setup_ai", fail)
    with pytest.raises(case.modules.main.AnalysisError, match="fallback report delivered"):
        case.agent.job(reraise=True)
    assert case.reports[0][1]["total_value"] == 1100


def test_both_channels_are_attempted_but_partial_delivery_fails(analysis_case, monkeypatch):
    case = analysis_case
    case.modules.config.NOTIFICATION_METHOD = "both"
    sent = []

    def fail(*a, **kw):
        raise RuntimeError("SMTP offline")

    monkeypatch.setattr(case.agent, "send_email", fail)
    monkeypatch.setattr(case.agent, "send_telegram", lambda text: sent.append(text))
    with pytest.raises(case.modules.main.ReportDeliveryError, match="email"):
        case.agent.job(reraise=True)
    assert len(sent) == 1
    assert "Synthetic analysis" in sent[0]


def test_notification_configuration_is_a_failure(agent_modules):
    agent = agent_modules.main.StockRiskAgent()
    agent_modules.config.SMTP_USER = ""
    with pytest.raises(agent_modules.main.ReportDeliveryError, match="not configured"):
        agent.send_report("test")


def test_missing_history_is_visible_and_cannot_imply_keep(analysis_case):
    case = analysis_case
    for key in ("hist_1y", "hist_3y", "hist_5y"):
        case.data[key] = pd.DataFrame()
    summary = case.agent.analyze_portfolio()
    stock = summary["stocks"][0]
    assert stock["scores"]["drawdown_profile"]["score"] >= 50
    assert not summary["data_complete"]
    assert summary["portfolio_level_metrics"] is None
    assert stock["action"] == "review"
    assert "DATA QUALITY" in case.reports[0][0]


def test_critical_alert_is_deterministic_even_if_ai_omits_it(analysis_case, monkeypatch):
    case = analysis_case
    rf = case.modules.main.rf
    for func in ("score_drawdown_profile", "score_downside_volatility", "score_liquidity_risk", "score_correlation_risk", "score_concentration_risk", "score_regime_sensitivity"):
        monkeypatch.setattr(rf, func, lambda *a, **kw: {"score": 10, "rating": "green", "details": {}})
    monkeypatch.setattr(rf, "score_balance_sheet", lambda *a: {"score": 100, "rating": "red", "details": {}})
    case.data["current_price"] = 90
    summary = case.agent.analyze_portfolio()
    stock = summary["stocks"][0]
    assert stock["composite"]["rating"] == "green"
    assert stock["action"] == "cut_loss_candidate"
    assert stock["trim_pct"] == 100
    assert "RISK ALERT" in case.reports[0][0]
    assert "CRITICAL CATEGORY: AAA: balance_sheet" in case.reports[0][0]


@pytest.mark.parametrize("interest, expected_action, expected_trim", [
    (0., "hold", 0),
    (None, "cut_loss_candidate", 50),
])
def test_zero_interest_is_complete_but_missing_interest_is_not(
    analysis_case, monkeypatch, interest, expected_action, expected_trim
):
    case = analysis_case
    rf = case.modules.main.rf
    for func in ("score_drawdown_profile", "score_downside_volatility", "score_liquidity_risk", "score_correlation_risk", "score_concentration_risk", "score_regime_sensitivity"):
        monkeypatch.setattr(rf, func, lambda *a, **kw: {"score": 10, "rating": "green", "details": {}})
    case.data["balance_sheet"].loc["Total Debt"] = 0.
    case.data["income_stmt"].loc["Interest Expense"] = interest
    case.data["current_price"] = 90.
    summary = case.agent.analyze_portfolio()
    stock = summary["stocks"][0]
    assert stock["composite"]["rating"] == "green"
    assert stock["composite"]["data_complete"] is (interest == 0.)
    assert stock["action"] == expected_action
    assert stock["trim_pct"] == expected_trim


def test_empty_history_is_retried_and_stale_history_rejected(agent_modules, history_clock):
    main = agent_modules.main
    end = pd.offsets.BDay().rollback(history_clock.tz_localize(None).normalize())
    fresh = pd.DataFrame({"Close": [100., 101.]}, index=pd.bdate_range(end=end, periods=2))
    answers = iter([pd.DataFrame(), fresh])
    ticker = types.SimpleNamespace(ticker="AAA", history=lambda **kw: next(answers))
    assert main._validated_history(ticker, "1y").equals(fresh)
    stale = pd.DataFrame({"Close": [100., 101.]}, index=pd.date_range("2020-01-01", periods=2))
    ticker.history = lambda **kw: stale
    with pytest.raises(ValueError, match="stale"):
        main._validated_history(ticker, "1y")

    # The next local business date is still in the future under the UTC policy.
    local_date = history_clock.tz_convert("Europe/London").tz_localize(None).normalize()
    future = pd.DataFrame({"Close": [100., 101.]}, index=pd.bdate_range(end=local_date, periods=2))
    ticker.history = lambda **kw: future
    with pytest.raises(ValueError, match="future-dated"):
        main._validated_history(ticker, "1y")


def test_price_selection_uses_valid_quote_then_recent_close(agent_modules, monkeypatch, history_clock):
    main = agent_modules.main
    end = pd.offsets.BDay().rollback(history_clock.tz_localize(None).normalize())
    history = pd.DataFrame({"Close": [100., 101.], "Volume": [10., 10.]}, index=pd.bdate_range(end=end, periods=2))
    ticker = types.SimpleNamespace(ticker="AAA", history=lambda **kw: history,
                                   info={"currency": "USD", "currentPrice": None, "regularMarketPrice": 105.},
                                   balance_sheet=pd.DataFrame(), income_stmt=pd.DataFrame())
    monkeypatch.setattr(main.yf, "Ticker", lambda *a: ticker, raising=False)
    agent = main.StockRiskAgent()
    assert agent.fetch_data("AAA")["current_price"] == 105
    ticker.info["regularMarketPrice"] = float("nan")
    data = agent.fetch_data("AAA")
    assert data["current_price"] == 101
    assert data["price_source"] == "latest_validated_close"


def test_smtp_refusal_is_not_reported_as_success(agent_modules, monkeypatch):
    main = agent_modules.main

    class SMTP:
        def __init__(self, host, port, timeout):
            assert timeout == 30

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def sendmail(self, *args):
            return {"test@example.invalid": (550, b"rejected")}

    monkeypatch.setattr(main.smtplib, "SMTP", SMTP)
    with pytest.raises(main.ReportDeliveryError, match="refused"):
        main.StockRiskAgent().send_report("Test")


def test_telegram_failure_propagates(agent_modules, monkeypatch):
    main = agent_modules.main
    agent_modules.config.NOTIFICATION_METHOD = "telegram"

    class Bot:
        def __init__(self, **kw):
            pass

        async def send_message(self, **kw):
            raise RuntimeError("Telegram offline")

    monkeypatch.setattr(main, "Bot", Bot)
    # This fake await completes synchronously. Avoid Windows asyncio's internal
    # loopback socket so the test can run with all socket connections blocked.
    def run_inline(coroutine):
        try:
            return coroutine.send(None)
        except StopIteration as result:
            return result.value
        finally:
            coroutine.close()

    monkeypatch.setattr(main.asyncio, "run", run_inline)
    with pytest.raises(main.ReportDeliveryError, match="Telegram offline"):
        main.StockRiskAgent().send_report("Test")


def test_alert_threshold_uses_unrounded_position_weight(analysis_case, monkeypatch):
    case = analysis_case
    case.frame = pd.DataFrame([{"Ticker": "AAA", "Position_Size": 4.96, "Avg_Price": 100},
                               {"Ticker": "BBB", "Position_Size": 95.04, "Avg_Price": 100}])
    rf = case.modules.main.rf
    for func in ("score_drawdown_profile", "score_downside_volatility", "score_liquidity_risk", "score_balance_sheet", "score_correlation_risk", "score_regime_sensitivity"):
        monkeypatch.setattr(rf, func, lambda *a, **kw: {"score": 10, "details": {}})
    monkeypatch.setattr(rf, "score_concentration_risk", lambda weight, score: {"score": 100 if weight < 5 else 10, "details": {}})
    summary = case.agent.analyze_portfolio()
    assert summary["stocks"][0]["composite"]["red_alert"] is True
    assert summary["stocks"][0]["position_pct"] == pytest.approx(4.96)
    assert summary["portfolio_red_alert"] is False


def test_provisional_report_is_visibly_labelled(analysis_case):
    case = analysis_case
    case.data["data_quality_issues"] = ["short history"]
    summary = case.agent.analyze_portfolio()
    html = case.agent.format_html_report(case.reports[0][0], summary)
    assert "Portfolio Composite (provisional)" in html
    assert "DATA QUALITY" in html
    assert "Candidate" in html
