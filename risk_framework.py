"""
risk_framework.py — Quantitative risk scoring engine for Proteus.

Pure math on pandas DataFrames + numpy. No Claude dependency.
Each scoring function returns: {"score": 0-100, "rating": "green/yellow/red", "details": {...}}
Composite: Green 0-35, Yellow 35-65, Red 65-100.
"""

import numpy as np
import pandas as pd
from collections import Counter


def _clamp(value, lo=0, hi=100):
    return max(lo, min(hi, value))


def normalize_daily_index(series):
    """Strip timezone and time-of-day from a daily series index.

    yfinance returns tz-aware timestamps in each exchange's local zone, so
    series from different markets (e.g. SPY vs an LSE stock) never share exact
    timestamps and silently align to an empty frame. Normalizing to naive
    dates makes cross-market correlation/beta calculations work.
    """
    if series is None or len(series) == 0:
        return series
    idx = series.index
    if isinstance(idx, pd.DatetimeIndex):
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        series = series.copy()
        series.index = idx.normalize()
    return series


def _rating(score):
    if score < 35:
        return "green"
    elif score < 65:
        return "yellow"
    return "red"


def _max_drawdown(prices):
    """Calculate maximum drawdown from a price series."""
    if prices is None or len(prices) < 2:
        return 0.0
    cummax = prices.cummax()
    drawdown = (prices - cummax) / cummax
    return abs(drawdown.min())


def _ulcer_index(prices, window=14):
    """Ulcer Index: RMS over a rolling window of percentage drawdowns from the running peak."""
    if prices is None or len(prices) < window:
        return 0.0
    cummax = prices.expanding().max()
    pct_dd = ((prices - cummax) / cummax) * 100
    ulcer = np.sqrt((pct_dd**2).rolling(window).mean()).iloc[-1]
    return ulcer if not np.isnan(ulcer) else 0.0


# ---------------------------------------------------------------------------
# 1. Drawdown Profile (20%)
# ---------------------------------------------------------------------------
def score_drawdown_profile(hist_1y, hist_3y, hist_5y):
    """Score based on max drawdown 1yr/3yr, drawdown from 5y high, Ulcer Index."""
    details = {}

    close_1y = hist_1y["Close"] if hist_1y is not None and len(hist_1y) > 0 else None
    close_3y = hist_3y["Close"] if hist_3y is not None and len(hist_3y) > 0 else None
    close_5y = hist_5y["Close"] if hist_5y is not None and len(hist_5y) > 0 else None

    dd_1y = _max_drawdown(close_1y) if close_1y is not None else 0
    dd_3y = _max_drawdown(close_3y) if close_3y is not None else 0

    # Current drawdown from 5y high. Capped lookback: a stock permanently below
    # a decades-old bubble peak shouldn't carry elevated drawdown risk forever.
    if close_5y is not None and len(close_5y) > 0:
        recent = close_5y.tail(252 * 5)
        high_5y = recent.max()
        current = recent.iloc[-1]
        dd_from_high = (high_5y - current) / high_5y if high_5y > 0 else 0
    else:
        dd_from_high = 0

    # Ulcer Index on 1yr data
    ulcer = _ulcer_index(close_1y) if close_1y is not None else 0

    details["max_dd_1y"] = round(dd_1y * 100, 2)
    details["max_dd_3y"] = round(dd_3y * 100, 2)
    details["dd_from_5y_high"] = round(dd_from_high * 100, 2)
    details["ulcer_index"] = round(ulcer, 2)

    # Drawdown blend (weights sum to 1.0), mapped so that
    # 10% DD → ~25 score, 30% DD → ~75, 50%+ DD → 100
    dd_blend = dd_1y * 0.4 + dd_3y * 0.3 + dd_from_high * 0.3
    dd_score = _clamp(dd_blend * 100 * 2.5)
    # Ulcer: ~5 → mild, ~10 → elevated, 20+ → severe sustained drawdowns
    ulcer_score = _clamp(ulcer * 5)

    raw_score = dd_score * 0.80 + ulcer_score * 0.20
    score = _clamp(round(raw_score))
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# 2. Downside Volatility (20%)
# ---------------------------------------------------------------------------
def score_downside_volatility(returns, spy_returns, risk_free_rate=0.04):
    """Score based on downside deviation, Sortino, down-market beta, CVaR 5%."""
    details = {}

    if returns is None or len(returns) < 20:
        return {"score": 50, "rating": "yellow", "details": {"data_available": False}}

    daily_rfr = risk_free_rate / 252
    excess = returns - daily_rfr

    # Downside deviation (annualized) — sqrt(mean(min(r, 0)^2))
    downside_sq = returns.clip(upper=0) ** 2
    downside_dev = np.sqrt(downside_sq.mean()) * np.sqrt(252) if len(returns) > 0 else 0
    details["downside_deviation"] = round(downside_dev * 100, 2)

    # Sortino ratio (annualized)
    ann_return = returns.mean() * 252
    sortino = (ann_return - risk_free_rate) / downside_dev if downside_dev > 0 else 0
    details["sortino_ratio"] = round(sortino, 2)

    # Down-market beta
    if spy_returns is not None and len(spy_returns) >= 20:
        aligned = pd.DataFrame({"stock": returns, "spy": spy_returns}).dropna()
        down_days = aligned[aligned["spy"] < 0]
        if len(down_days) > 5:
            cov = np.cov(down_days["stock"], down_days["spy"])
            spy_var = cov[1][1]
            down_beta = cov[0][1] / spy_var if spy_var > 0 else 1.0
        else:
            down_beta = 1.0
    else:
        down_beta = 1.0
    details["down_market_beta"] = round(down_beta, 2)

    # CVaR 5% (expected shortfall)
    sorted_returns = returns.sort_values()
    cutoff = max(1, int(len(sorted_returns) * 0.05))
    cvar = sorted_returns.iloc[:cutoff].mean()
    details["cvar_5pct"] = round(cvar * 100, 2)

    # Score components
    # Downside dev: 15% annualized → ~25, 30% → ~60, 50%+ → ~100
    dd_score = _clamp(downside_dev * 100 * 2)
    # Sortino: >1.5 → low risk, <0 → high risk
    sortino_score = _clamp(50 - sortino * 25)
    # Down beta: 1.0 → neutral, >1.5 → bad, <0.5 → good
    beta_score = _clamp((down_beta - 0.5) * 50)
    # CVaR: -2% → moderate, -5%+ → bad
    cvar_score = _clamp(abs(cvar) * 100 * 10)

    raw = dd_score * 0.30 + sortino_score * 0.30 + beta_score * 0.20 + cvar_score * 0.20
    score = _clamp(round(raw))
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# 3. Liquidity Risk (10%)
# ---------------------------------------------------------------------------
def score_liquidity_risk(position_size, price, avg_volume, bid, ask, vol_30d, vol_90d):
    """Score based on days to liquidate, bid-ask spread, volume trend."""
    details = {}

    # Days to liquidate (assume 10% of avg daily volume)
    if avg_volume and avg_volume > 0 and price and price > 0:
        daily_capacity = avg_volume * 0.10 * price
        position_value = position_size * price
        days_to_liq = position_value / daily_capacity if daily_capacity > 0 else 99
    else:
        days_to_liq = 99
    details["days_to_liquidate"] = round(days_to_liq, 2)

    # Bid-ask spread
    if bid and ask and bid > 0:
        spread_pct = (ask - bid) / bid * 100
    elif ask and ask > 0 and (bid is None or bid == 0):
        # Bid is zero/missing but ask exists — extremely illiquid
        spread_pct = 10.0
    else:
        spread_pct = 0
    details["bid_ask_spread_pct"] = round(spread_pct, 2)

    # Volume trend
    if vol_30d and vol_90d and vol_90d > 0:
        vol_trend = vol_30d / vol_90d
    else:
        vol_trend = 1.0
    details["volume_trend_30d_90d"] = round(vol_trend, 2)

    # Score: days to liq (0.5 → low, 5+ → high), spread (0.1% → low, 2%+ → high)
    liq_score = _clamp(days_to_liq * 15)
    spread_score = _clamp(spread_pct * 25)
    # Volume decline: trend < 0.7 → concerning
    vol_score = _clamp((1.0 - vol_trend) * 100) if vol_trend < 1.0 else 0

    raw = liq_score * 0.45 + spread_score * 0.35 + vol_score * 0.20
    score = _clamp(round(raw))
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# 4. Balance Sheet Fragility (15%)
# ---------------------------------------------------------------------------
def score_balance_sheet(balance_sheet_df, income_stmt_df):
    """Score based on Debt/EBITDA, current ratio, interest coverage."""
    details = {"data_available": True}

    # Extract values safely
    def _get_latest(df, field):
        if df is None or df.empty:
            return None
        if field in df.index:
            val = df.loc[field].iloc[0]
            return float(val) if pd.notna(val) else None
        return None

    ebitda = _get_latest(income_stmt_df, "EBITDA")
    interest = _get_latest(income_stmt_df, "Interest Expense")
    total_debt = _get_latest(balance_sheet_df, "Total Debt")
    current_assets = _get_latest(balance_sheet_df, "Current Assets")
    current_liab = _get_latest(balance_sheet_df, "Current Liabilities")

    # If no financial data at all, return neutral
    if ebitda is None and total_debt is None and current_assets is None:
        return {"score": 50, "rating": "yellow", "details": {"data_available": False}}

    scores = []

    # Debt/EBITDA
    if total_debt is not None and ebitda is not None and ebitda > 0:
        debt_ebitda = total_debt / ebitda
        details["debt_to_ebitda"] = round(debt_ebitda, 2)
        # <2 → good, 2-4 → moderate, >4 → bad, >6 → very bad
        scores.append(_clamp(debt_ebitda * 15))
    elif total_debt is not None and (ebitda is None or ebitda <= 0):
        details["debt_to_ebitda"] = None
        scores.append(80)  # Has debt but no positive EBITDA — risky
    else:
        details["debt_to_ebitda"] = None

    # Current ratio
    if current_assets is not None and current_liab is not None and current_liab > 0:
        current_ratio = current_assets / current_liab
        details["current_ratio"] = round(current_ratio, 2)
        # >2 → great, 1-2 → okay, <1 → bad
        cr_score = _clamp(100 - current_ratio * 40)
        scores.append(cr_score)
    else:
        details["current_ratio"] = None

    # Interest coverage
    if ebitda is not None and interest is not None and interest != 0:
        interest_cov = ebitda / abs(interest)
        details["interest_coverage"] = round(interest_cov, 2)
        # >5 → great, 2-5 → okay, <2 → bad
        ic_score = _clamp(80 - interest_cov * 10)
        scores.append(ic_score)
    else:
        details["interest_coverage"] = None

    if scores:
        score = _clamp(round(sum(scores) / len(scores)))
    else:
        score = 50
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# 5. Correlation Risk (15%)
# ---------------------------------------------------------------------------
def score_correlation_risk(
    stock_returns, portfolio_returns, spy_returns, sector, sector_weights
):
    """Score based on SPY correlation, portfolio correlation, down-market correlation, sector concentration.

    portfolio_returns should EXCLUDE this stock (leave-one-out) — otherwise a
    large position mechanically correlates with the portfolio it dominates.
    """
    details = {}

    if stock_returns is None or len(stock_returns) < 30:
        return {"score": 50, "rating": "yellow", "details": {"data_available": False}}

    # Rolling 60d correlation with SPY
    if spy_returns is not None and len(spy_returns) >= 60:
        aligned = pd.DataFrame({"stock": stock_returns, "spy": spy_returns}).dropna()
        if len(aligned) >= 60:
            rolling_corr = aligned["stock"].rolling(60).corr(aligned["spy"])
            avg_corr = rolling_corr.dropna().mean()
        else:
            avg_corr = aligned["stock"].corr(aligned["spy"])
    else:
        avg_corr = 0.5
    details["avg_spy_correlation"] = (
        round(avg_corr, 2) if not np.isnan(avg_corr) else 0.5
    )

    # Correlation with rest of portfolio
    if portfolio_returns is not None and len(portfolio_returns) >= 30:
        aligned = pd.DataFrame(
            {"stock": stock_returns, "port": portfolio_returns}
        ).dropna()
        if len(aligned) >= 30:
            port_corr = aligned["stock"].corr(aligned["port"])
            port_corr = round(port_corr, 2) if not np.isnan(port_corr) else 0.5
        else:
            port_corr = 0.5
    else:
        port_corr = 0.5
    details["portfolio_correlation"] = port_corr

    # Down-market correlation
    if spy_returns is not None:
        aligned = pd.DataFrame({"stock": stock_returns, "spy": spy_returns}).dropna()
        down_days = aligned[aligned["spy"] < 0]
        if len(down_days) > 10:
            down_corr = down_days["stock"].corr(down_days["spy"])
            down_corr = round(down_corr, 2) if not np.isnan(down_corr) else 0.5
        else:
            down_corr = avg_corr
    else:
        down_corr = 0.5
    details["down_market_correlation"] = down_corr

    # Sector concentration: this stock's sector weight in the portfolio.
    # (Portfolio-wide HHI is identical for every stock, so it can't
    # differentiate holdings — it's reported at portfolio level instead.)
    if sector_weights and sector:
        same_sector_wt = sector_weights.get(sector, 0)
        details["same_sector_pct"] = round(same_sector_wt * 100, 1)
        # 25% of portfolio in this sector → 40, 40% → 64, 50%+ → 80+
        sector_score = _clamp(same_sector_wt * 160)
    else:
        details["same_sector_pct"] = None
        sector_score = 50  # sector unknown → neutral

    # Score
    # High SPY correlation → less diversification benefit → higher risk
    corr_score = _clamp(avg_corr * 70)
    # High portfolio correlation → concentrated risk
    port_corr_score = _clamp(port_corr * 60)
    # Down-market correlation penalized more
    down_corr_score = _clamp(down_corr * 80)

    raw = (
        corr_score * 0.25
        + port_corr_score * 0.20
        + down_corr_score * 0.30
        + sector_score * 0.25
    )
    score = _clamp(round(raw))
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# 6. Concentration Risk (10%)
# ---------------------------------------------------------------------------
def score_concentration_risk(position_pct, individual_score):
    """Score based on position %, risk-weighted concentration."""
    details = {}

    details["position_pct"] = round(position_pct, 2)

    # Risk-adjusted concentration: large position + high individual risk = very bad
    risk_weighted = position_pct * (individual_score / 100)
    details["risk_weighted_concentration"] = round(risk_weighted, 2)

    # >25% single position → high score, >40% → very high
    pos_score = _clamp(position_pct * 2.5)
    risk_adj_score = _clamp(risk_weighted * 5)

    raw = pos_score * 0.50 + risk_adj_score * 0.50
    score = _clamp(round(raw))
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# 7. Regime Sensitivity (10%)
# ---------------------------------------------------------------------------
def score_regime_sensitivity(returns, tnx_changes, vix_series):
    """Score based on performance during rate spikes and VIX crises.

    A regime with too few observed stress days is treated as UNTESTED
    (excluded), not safe — if neither regime has enough stress history the
    score is neutral 50 rather than 0. Pass multi-year returns so the window
    actually contains past stress events.
    """
    details = {}

    if returns is None or len(returns) < 30:
        return {"score": 50, "rating": "yellow", "details": {"data_available": False}}

    component_scores = []

    # Rate spike regime: days when TNX rises >2 std devs
    rate_spike_return = None
    if tnx_changes is not None and len(tnx_changes) > 30:
        aligned = pd.DataFrame({"stock": returns, "tnx": tnx_changes}).dropna()
        tnx_std = aligned["tnx"].std()
        if tnx_std > 0:
            spike_days = aligned[aligned["tnx"] > 2 * tnx_std]
            if len(spike_days) > 3:
                rate_spike_return = spike_days["stock"].mean() * 100
                # -0.5%/day during spikes → moderate, -2%+ → very bad
                component_scores.append(_clamp(abs(min(rate_spike_return, 0)) * 30))
    details["avg_return_rate_spike_pct"] = (
        round(rate_spike_return, 3) if rate_spike_return is not None else None
    )

    # VIX crisis regime: days when VIX > 25
    vix_crisis_return = None
    if vix_series is not None and len(vix_series) > 0:
        aligned = pd.DataFrame({"stock": returns, "vix": vix_series}).dropna()
        crisis_days = aligned[aligned["vix"] > 25]
        if len(crisis_days) > 3:
            vix_crisis_return = crisis_days["stock"].mean() * 100
            component_scores.append(_clamp(abs(min(vix_crisis_return, 0)) * 30))
    details["avg_return_vix_crisis_pct"] = (
        round(vix_crisis_return, 3) if vix_crisis_return is not None else None
    )

    if not component_scores:
        details["insufficient_stress_history"] = True
        return {"score": 50, "rating": "yellow", "details": details}

    score = _clamp(round(sum(component_scores) / len(component_scores)))
    return {"score": score, "rating": _rating(score), "details": details}


# ---------------------------------------------------------------------------
# Portfolio-level metrics
# ---------------------------------------------------------------------------
def score_portfolio_level(portfolio_returns, spy_returns, risk_free_rate=0.04):
    """Risk metrics computed on the aggregated portfolio return stream.

    The composite is a weighted average of per-stock scores, which is blind to
    diversification — ten uncorrelated risky stocks and one concentrated bet
    can average the same. Scoring the actual portfolio returns captures it.
    Returns None if there is not enough data.
    """
    if portfolio_returns is None or len(portfolio_returns) < 20:
        return None

    prices = (1 + portfolio_returns).cumprod()
    downside = score_downside_volatility(portfolio_returns, spy_returns, risk_free_rate)

    return {
        "max_drawdown_1y_pct": round(_max_drawdown(prices) * 100, 2),
        "downside_volatility_score": downside["score"],
        "downside_details": downside["details"],
    }


# ---------------------------------------------------------------------------
# Composite Score
# ---------------------------------------------------------------------------
WEIGHTS = {
    "drawdown_profile": 0.20,
    "downside_volatility": 0.20,
    "liquidity_risk": 0.10,
    "balance_sheet": 0.15,
    "correlation_risk": 0.15,
    "concentration_risk": 0.10,
    "regime_sensitivity": 0.10,
}


def portfolio_red_alert(all_scores, portfolio_composite, min_position_pct=5.0):
    """Portfolio-wide red alert.

    A stock's own red alert only escalates to the portfolio level if the
    position is at least min_position_pct of the portfolio — a 1% speculative
    position going red shouldn't flag the whole portfolio. The portfolio
    composite check is ungated.
    """
    significant_stock_alert = any(
        s["composite"]["red_alert"] and s["position_pct"] >= min_position_pct
        for s in all_scores
    )
    return significant_stock_alert or portfolio_composite > 65


def calculate_composite(scores_dict):
    """
    Calculate weighted composite score from individual category scores.

    scores_dict: {"drawdown_profile": {"score": X, ...}, "downside_volatility": {...}, ...}
    Returns: {"composite_score": 0-100, "rating": ..., "red_alert": bool, "category_scores": {...}}
    """
    weighted_sum = 0
    category_summary = {}
    any_critical = False

    for category, weight in WEIGHTS.items():
        cat_data = scores_dict.get(category, {"score": 50})
        cat_score = cat_data.get("score", 50)
        weighted_sum += cat_score * weight
        category_summary[category] = {
            "score": cat_score,
            "rating": cat_data.get("rating", _rating(cat_score)),
            "weight": f"{int(weight * 100)}%",
        }
        if cat_score > 80:
            any_critical = True

    composite = _clamp(round(weighted_sum))
    red_alert = any_critical or composite > 65

    return {
        "composite_score": composite,
        "rating": _rating(composite),
        "red_alert": red_alert,
        "category_scores": category_summary,
    }
