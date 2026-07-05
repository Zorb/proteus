"""Unit tests for risk_framework.py — pure scoring math, no network."""

import numpy as np
import pandas as pd
import pytest

import risk_framework as rf


def _price_series(prices, tz=None):
    idx = pd.date_range("2025-01-01", periods=len(prices), freq="B", tz=tz)
    return pd.Series(prices, index=idx, dtype=float)


def _hist(prices, tz=None):
    close = _price_series(prices, tz=tz)
    return pd.DataFrame({"Close": close})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_clamp(self):
        assert rf._clamp(-5) == 0
        assert rf._clamp(50) == 50
        assert rf._clamp(150) == 100

    def test_rating_boundaries(self):
        assert rf._rating(0) == "green"
        assert rf._rating(34.9) == "green"
        assert rf._rating(35) == "yellow"
        assert rf._rating(64.9) == "yellow"
        assert rf._rating(65) == "red"
        assert rf._rating(100) == "red"

    def test_max_drawdown_known_value(self):
        # 100 → 120 → 60: drawdown from 120 to 60 = 50%
        prices = _price_series([100, 120, 60, 80])
        assert rf._max_drawdown(prices) == pytest.approx(0.5)

    def test_max_drawdown_monotonic_rise_is_zero(self):
        prices = _price_series([100, 110, 120, 130])
        assert rf._max_drawdown(prices) == 0

    def test_max_drawdown_short_series(self):
        assert rf._max_drawdown(None) == 0.0
        assert rf._max_drawdown(_price_series([100])) == 0.0

    def test_normalize_daily_index_strips_tz_and_time(self):
        s = pd.Series(
            [1.0, 2.0],
            index=pd.DatetimeIndex(
                ["2025-01-02 16:00", "2025-01-03 16:00"], tz="America/New_York"
            ),
        )
        out = rf.normalize_daily_index(s)
        assert out.index.tz is None
        assert list(out.index) == [
            pd.Timestamp("2025-01-02"),
            pd.Timestamp("2025-01-03"),
        ]

    def test_normalize_daily_index_aligns_cross_market_series(self):
        ny = pd.Series(
            [0.01, 0.02],
            index=pd.DatetimeIndex(
                ["2025-01-02 16:00", "2025-01-03 16:00"], tz="America/New_York"
            ),
        )
        ldn = pd.Series(
            [0.03, 0.04],
            index=pd.DatetimeIndex(
                ["2025-01-02 16:30", "2025-01-03 16:30"], tz="Europe/London"
            ),
        )
        aligned = pd.DataFrame(
            {
                "a": rf.normalize_daily_index(ny),
                "b": rf.normalize_daily_index(ldn),
            }
        ).dropna()
        assert len(aligned) == 2

    def test_normalize_daily_index_handles_none_and_empty(self):
        assert rf.normalize_daily_index(None) is None
        empty = pd.Series(dtype=float)
        assert rf.normalize_daily_index(empty) is empty


# ---------------------------------------------------------------------------
# Category scores
# ---------------------------------------------------------------------------
class TestDrawdownProfile:
    def test_flat_prices_score_low(self):
        hist = _hist([100.0] * 300)
        result = rf.score_drawdown_profile(hist, hist, hist)
        assert result["score"] <= 10
        assert result["rating"] == "green"

    def test_crash_scores_high(self):
        prices = list(np.linspace(100, 200, 150)) + list(np.linspace(200, 80, 150))
        hist = _hist(prices)
        result = rf.score_drawdown_profile(hist, hist, hist)
        assert result["score"] >= 65
        assert result["rating"] == "red"

    def test_missing_data(self):
        result = rf.score_drawdown_profile(None, None, None)
        assert 0 <= result["score"] <= 100


class TestDownsideVolatility:
    def test_insufficient_data_returns_neutral(self):
        result = rf.score_downside_volatility(None, None)
        assert result["score"] == 50
        assert result["details"] == {"data_available": False}

    def test_steady_gains_score_low(self):
        returns = pd.Series(
            [0.001] * 252, index=pd.date_range("2025-01-01", periods=252, freq="B")
        )
        result = rf.score_downside_volatility(returns, None, risk_free_rate=0.04)
        assert result["score"] < 35

    def test_volatile_losses_score_high(self):
        rng = np.random.RandomState(42)
        returns = pd.Series(
            rng.normal(-0.005, 0.04, 252),
            index=pd.date_range("2025-01-01", periods=252, freq="B"),
        )
        result = rf.score_downside_volatility(returns, None, risk_free_rate=0.04)
        assert result["score"] > 65


class TestLiquidityRisk:
    def test_liquid_large_cap_scores_low(self):
        result = rf.score_liquidity_risk(
            position_size=100,
            price=150.0,
            avg_volume=50_000_000,
            bid=149.99,
            ask=150.01,
            vol_30d=50_000_000,
            vol_90d=50_000_000,
        )
        assert result["score"] < 35

    def test_missing_volume_scores_high(self):
        result = rf.score_liquidity_risk(
            position_size=100,
            price=150.0,
            avg_volume=None,
            bid=None,
            ask=None,
            vol_30d=None,
            vol_90d=None,
        )
        assert result["score"] >= 35
        assert result["details"]["days_to_liquidate"] == 99


class TestBalanceSheet:
    def test_no_data_returns_neutral(self):
        result = rf.score_balance_sheet(pd.DataFrame(), pd.DataFrame())
        assert result["score"] == 50
        assert result["details"] == {"data_available": False}

    def test_healthy_balance_sheet_scores_low(self):
        balance = pd.DataFrame(
            {"2025": [1_000_000, 5_000_000, 1_000_000]},
            index=["Total Debt", "Current Assets", "Current Liabilities"],
        )
        income = pd.DataFrame(
            {"2025": [10_000_000, 100_000]},
            index=["EBITDA", "Interest Expense"],
        )
        result = rf.score_balance_sheet(balance, income)
        assert result["score"] < 35

    def test_debt_without_ebitda_scores_high(self):
        balance = pd.DataFrame({"2025": [10_000_000]}, index=["Total Debt"])
        income = pd.DataFrame({"2025": [-500_000]}, index=["EBITDA"])
        result = rf.score_balance_sheet(balance, income)
        assert result["score"] >= 65


class TestConcentrationRisk:
    def test_small_safe_position_scores_low(self):
        result = rf.score_concentration_risk(position_pct=5, individual_score=20)
        assert result["score"] < 35

    def test_large_risky_position_scores_high(self):
        result = rf.score_concentration_risk(position_pct=40, individual_score=80)
        assert result["score"] >= 65

    def test_monotonic_in_position_size(self):
        small = rf.score_concentration_risk(10, 50)["score"]
        large = rf.score_concentration_risk(30, 50)["score"]
        assert large > small


class TestCorrelationRisk:
    def test_insufficient_data_returns_neutral(self):
        result = rf.score_correlation_risk(None, None, None, None, {})
        assert result["score"] == 50


class TestRegimeSensitivity:
    def test_insufficient_data_returns_neutral(self):
        result = rf.score_regime_sensitivity(None, None, None)
        assert result["score"] == 50


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
class TestComposite:
    def test_weights_sum_to_one(self):
        assert sum(rf.WEIGHTS.values()) == pytest.approx(1.0)

    def test_all_green_composite(self):
        scores = {cat: {"score": 10, "rating": "green"} for cat in rf.WEIGHTS}
        result = rf.calculate_composite(scores)
        assert result["composite_score"] == 10
        assert result["rating"] == "green"
        assert result["red_alert"] is False

    def test_red_alert_on_single_critical_category(self):
        scores = {cat: {"score": 10, "rating": "green"} for cat in rf.WEIGHTS}
        scores["liquidity_risk"] = {"score": 85, "rating": "red"}
        result = rf.calculate_composite(scores)
        assert result["red_alert"] is True

    def test_red_alert_on_high_composite(self):
        scores = {cat: {"score": 70, "rating": "red"} for cat in rf.WEIGHTS}
        result = rf.calculate_composite(scores)
        assert result["composite_score"] == 70
        assert result["red_alert"] is True

    def test_missing_category_defaults_neutral(self):
        result = rf.calculate_composite({})
        assert result["composite_score"] == 50
        assert result["red_alert"] is False
