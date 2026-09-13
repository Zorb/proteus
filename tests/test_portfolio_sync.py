import pandas as pd
import pytest


def statement(rows=""):
    return f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="test"><OpenPositions>{rows}</OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>'


def position(qty="10", cost="100", **attrs):
    fields = dict(assetCategory="STK", symbol="AAA", position=qty, costBasisPrice=cost,
                  currency="USD", listingExchange="NYSE", levelOfDetail="SUMMARY")
    fields.update(attrs)
    return '<OpenPosition ' + ' '.join(f'{key}="{value}"' for key, value in fields.items()) + '/>'


def test_valid_empty_is_distinct_from_invalid_statement(agent_modules, monkeypatch):
    sync, cfg = agent_modules.sync, agent_modules.config
    cfg.IBKR_FLEX_TOKEN = cfg.IBKR_FLEX_QUERY_ID = "test"
    monkeypatch.setattr(sync, "_send_request", lambda *a: "ref")
    monkeypatch.setattr(sync, "_get_statement", lambda *a: statement())
    result = sync.fetch_ibkr_portfolio()
    assert result is not None and result.empty
    for xml in ("broken XML", "<FlexStatementResponse/>", "<FlexQueryResponse><FlexStatements><FlexStatement/></FlexStatements></FlexQueryResponse>"):
        assert sync._parse_positions(xml) is None


def test_lots_are_consolidated_with_weighted_entry(agent_modules):
    sync = agent_modules.sync
    rows = sync._parse_positions(statement(position("10", "100", levelOfDetail="LOT") + position("30", "200", levelOfDetail="LOT")))
    result = sync.normalize_positions(pd.DataFrame(rows))
    assert len(result) == 1
    assert result.iloc[0]["Position_Size"] == 40
    assert result.iloc[0]["Avg_Price"] == 175


def test_summary_prevents_double_counting_its_lots(agent_modules):
    sync = agent_modules.sync
    xml = statement(position("40", "175") + position("10", "100", levelOfDetail="LOT") + position("30", "200", levelOfDetail="LOT"))
    result = sync.normalize_positions(pd.DataFrame(sync._parse_positions(xml)))
    assert result.iloc[0]["Position_Size"] == 40


def test_duplicate_summaries_are_rejected(agent_modules):
    with pytest.raises(agent_modules.sync.PortfolioValidationError, match="duplicate summaries"):
        agent_modules.sync._parse_positions(statement(position() + position()))


@pytest.mark.parametrize("quantity", [-1, float("nan"), float("inf"), "bad"])
def test_bad_or_short_quantity_is_rejected(agent_modules, quantity):
    frame = pd.DataFrame([{"Ticker": "AAA", "Position_Size": quantity, "Avg_Price": 100}])
    with pytest.raises(agent_modules.sync.PortfolioValidationError):
        agent_modules.sync.normalize_positions(frame)


@pytest.mark.parametrize("currency", ["GBP", "GBp", "", None])
def test_non_usd_broker_currency_is_rejected(agent_modules, currency):
    frame = pd.DataFrame([{"Ticker": "AAA", "Position_Size": 10, "Avg_Price": 100, "Currency": currency}])
    with pytest.raises(agent_modules.sync.PortfolioValidationError, match="USD"):
        agent_modules.sync.normalize_positions(frame)


def test_legacy_csv_defaults_usd_and_missing_cost_is_not_zero(agent_modules):
    frame = pd.DataFrame([{"Ticker": " aaa ", "Position_Size": 10, "Avg_Price": 100},
                          {"Ticker": "AAA", "Position_Size": 20, "Avg_Price": float("nan")}])
    result = agent_modules.sync.normalize_positions(frame)
    assert result.iloc[0]["Ticker"] == "AAA"
    assert result.iloc[0]["Position_Size"] == 30
    assert result.iloc[0]["Currency"] == "USD"
    assert pd.isna(result.iloc[0]["Avg_Price"])


def test_multiple_accounts_can_be_combined(agent_modules):
    sync = agent_modules.sync
    rows = sync._parse_positions(statement(position(accountId="one") + position(accountId="two")))
    result = sync.normalize_positions(pd.DataFrame(rows))
    assert result.iloc[0]["Position_Size"] == 20


def test_missing_asset_category_cannot_look_like_empty_account(agent_modules):
    xml = statement('<OpenPosition symbol="AAA" position="10"/>')
    with pytest.raises(agent_modules.sync.PortfolioValidationError, match="AssetCategory"):
        agent_modules.sync._parse_positions(xml)


def test_lots_from_different_statement_dates_are_rejected(agent_modules):
    xml = statement(position(levelOfDetail="LOT", reportDate="20260910") + position(levelOfDetail="LOT", reportDate="20260911"))
    with pytest.raises(agent_modules.sync.PortfolioValidationError, match="report dates"):
        agent_modules.sync._parse_positions(xml)


@pytest.mark.parametrize("second_account", ["test", "another"])
def test_different_stocks_must_share_a_snapshot_date(agent_modules, second_account):
    xml = statement(position(reportDate="20260910") +
                    position(symbol="BBB", accountId=second_account, reportDate="20260911"))
    with pytest.raises(agent_modules.sync.PortfolioValidationError, match="report dates"):
        agent_modules.sync._parse_positions(xml)


def test_different_stocks_on_the_same_date_are_accepted(agent_modules):
    sync = agent_modules.sync
    xml = statement(position(reportDate="20260911") +
                    position(symbol="BBB", reportDate="20260911"))
    result = sync.normalize_positions(pd.DataFrame(sync._parse_positions(xml)))
    assert result["Ticker"].tolist() == ["AAA", "BBB"]
    assert result["Position_Size"].tolist() == [10., 10.]


def test_statement_dates_prevent_combining_disjoint_old_holdings(agent_modules):
    xml = ('<FlexQueryResponse><FlexStatements>'
           '<FlexStatement accountId="test" toDate="20260910"><OpenPositions>' +
           position() + '</OpenPositions></FlexStatement>'
           '<FlexStatement accountId="test" toDate="20260911"><OpenPositions>' +
           position(symbol="BBB") + '</OpenPositions></FlexStatement>'
           '</FlexStatements></FlexQueryResponse>')
    with pytest.raises(agent_modules.sync.PortfolioValidationError, match="report dates"):
        agent_modules.sync._parse_positions(xml)
