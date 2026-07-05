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

    def test_calibration_matches_documented_curve(self):
        # A uniform ~10% drawdown should land near 25 (documented mapping),
        # and ~30% near 75. Ulcer adds a bit on top for sustained drawdowns.
        dip_10 = _hist([100.0] * 100 + [90.0] * 100)
        result_10 = rf.score_drawdown_profile(dip_10, dip_10, dip_10)
        assert 18 <= result_10["score"] <= 35

        dip_30 = _hist([100.0] * 100 + [70.0] * 100)
        result_30 = rf.score_drawdown_profile(dip_30, dip_30, dip_30)
        assert 60 <= result_30["score"] <= 85

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

    def test_sector_weight_differentiates_stocks(self):
        # Same return stream, different sector exposure: the stock in the
        # dominant sector must score higher than the one in the small sector
        rng = np.random.RandomState(7)
        returns = pd.Series(
            rng.normal(0, 0.01, 120),
            index=pd.date_range("2025-01-01", periods=120, freq="B"),
        )
        weights = {"Technology": 0.7, "Utilities": 0.1}
        tech = rf.score_correlation_risk(returns, None, None, "Technology", weights)
        util = rf.score_correlation_risk(returns, None, None, "Utilities", weights)
        assert tech["score"] > util["score"]
        assert tech["details"]["same_sector_pct"] == 70.0
        assert util["details"]["same_sector_pct"] == 10.0

    def test_unknown_sector_is_neutral(self):
        rng = np.random.RandomState(7)
        returns = pd.Series(
            rng.normal(0, 0.01, 120),
            index=pd.date_range("2025-01-01", periods=120, freq="B"),
        )
        result = rf.score_correlation_risk(returns, None, None, None, {})
        assert result["details"]["same_sector_pct"] is None


class TestRegimeSensitivity:
    def _stock_returns(self, values):
        return pd.Series(
            values, index=pd.date_range("2025-01-01", periods=len(values), freq="B")
        )

    def test_insufficient_data_returns_neutral(self):
        result = rf.score_regime_sensitivity(None, None, None)
        assert result["score"] == 50

    def test_calm_market_is_untested_not_safe(self):
        # No VIX>25 days and no rate spikes in the window: the stock is
        # untested, so the score must be neutral 50, not 0 (safest)
        returns = self._stock_returns([0.001] * 100)
        tnx = self._stock_returns([0.0] * 100)  # zero std → no spike days
        vix = self._stock_returns([15.0] * 100)  # never above 25
        result = rf.score_regime_sensitivity(returns, tnx, vix)
        assert result["score"] == 50
        assert result["details"]["insufficient_stress_history"] is True
        assert result["details"]["avg_return_vix_crisis_pct"] is None

    def test_losses_during_vix_crisis_score_high(self):
        # Stock drops 2% on each of 10 crisis days
        stock_vals = [0.001] * 100
        vix_vals = [15.0] * 100
        for i in range(10, 20):
            stock_vals[i] = -0.02
            vix_vals[i] = 30.0
        returns = self._stock_returns(stock_vals)
        vix = self._stock_returns(vix_vals)
        result = rf.score_regime_sensitivity(returns, None, vix)
        assert result["score"] >= 60
        assert result["details"]["avg_return_vix_crisis_pct"] == -2.0

    def test_gains_during_crisis_score_low(self):
        stock_vals = [0.001] * 100
        vix_vals = [15.0] * 100
        for i in range(10, 20):
            stock_vals[i] = 0.01
            vix_vals[i] = 30.0
        returns = self._stock_returns(stock_vals)
        vix = self._stock_returns(vix_vals)
        result = rf.score_regime_sensitivity(returns, None, vix)
        assert result["score"] == 0


# ---------------------------------------------------------------------------
# Portfolio-level metrics
# ---------------------------------------------------------------------------
class TestPortfolioLevel:
    def test_insufficient_data_returns_none(self):
        assert rf.score_portfolio_level(None, None) is None
        short = pd.Series([0.01] * 5)
        assert rf.score_portfolio_level(short, None) is None

    def test_diversification_shows_in_portfolio_drawdown(self):
        # Two anti-correlated streams: each leg has a real drawdown, but the
        # 50/50 portfolio is nearly flat — portfolio-level metrics must
        # reflect the hedged stream, not the average of the legs
        idx = pd.date_range("2025-01-01", periods=100, freq="B")
        rng = np.random.RandomState(3)
        leg = rng.normal(0, 0.02, 100)
        a = pd.Series(leg, index=idx)
        b = pd.Series(-leg, index=idx)
        portfolio = (a + b) / 2
        result = rf.score_portfolio_level(portfolio, None, risk_free_rate=0.04)
        leg_dd = rf._max_drawdown((1 + a).cumprod()) * 100
        assert result["max_drawdown_1y_pct"] == 0.0
        assert leg_dd > 5  # sanity: the individual leg really was risky


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
