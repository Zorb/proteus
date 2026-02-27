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
    """Ulcer Index: RMS of percentage drawdowns over rolling window."""
    if prices is None or len(prices) < window:
        return 0.0
    cummax = prices.expanding().max()
    pct_dd = ((prices - cummax) / cummax) * 100
    ulcer = np.sqrt((pct_dd**2).rolling(window).mean()).iloc[-1]
    return ulcer if not np.isnan(ulcer) else 0.0


# ---------------------------------------------------------------------------
# 1. Drawdown Profile (20%)
# ---------------------------------------------------------------------------
def score_drawdown_profile(hist_1y, hist_3y, hist_max):
    """Score based on max drawdown 1yr/3yr, drawdown from ATH, Ulcer Index."""
    details = {}

    # Max drawdown 1yr
    close_1y = hist_1y["Close"] if hist_1y is not None and len(hist_1y) > 0 else None
    close_3y = hist_3y["Close"] if hist_3y is not None and len(hist_3y) > 0 else None
    close_max = (
        hist_max["Close"] if hist_max is not None and len(hist_max) > 0 else None
    )

    dd_1y = _max_drawdown(close_1y) if close_1y is not None else 0
    dd_3y = _max_drawdown(close_3y) if close_3y is not None else 0

    # Current drawdown from ATH
    if close_max is not None and len(close_max) > 0:
        ath = close_max.max()
        current = close_max.iloc[-1]
        dd_from_ath = (ath - current) / ath if ath > 0 else 0
    else:
        dd_from_ath = 0

    # Ulcer Index on 1yr data
    ulcer = _ulcer_index(close_1y) if close_1y is not None else 0

    details["max_dd_1y"] = round(dd_1y * 100, 2)
    details["max_dd_3y"] = round(dd_3y * 100, 2)
    details["dd_from_ath"] = round(dd_from_ath * 100, 2)
    details["ulcer_index"] = round(ulcer, 2)

    # Score: weighted blend of drawdown metrics, mapped 0-100
    # 10% DD → ~25 score, 30% DD → ~75, 50%+ DD → ~100
    dd_blend = (dd_1y * 0.35 + dd_3y * 0.25 + dd_from_ath * 0.25) * 100
    ulcer_contrib = min(ulcer * 2, 30)  # Ulcer capped at 30 points contribution
    raw_score = dd_blend * 2.0 + ulcer_contrib * 0.15

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
    """Score based on SPY correlation, portfolio correlation, down-market correlation, sector concentration."""
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

    # Sector concentration (value-weighted HHI)
    if sector_weights:
        hhi = sum(w**2 for w in sector_weights.values())
        details["sector_hhi"] = round(hhi, 3)
        # This stock's sector weight in portfolio
        same_sector_wt = sector_weights.get(sector, 0) if sector else 0
        details["same_sector_pct"] = round(same_sector_wt * 100, 1)
    else:
        hhi = 0.5
        same_sector_wt = 0
        details["sector_hhi"] = None
        details["same_sector_pct"] = None

    # Score
    # High SPY correlation → less diversification benefit → higher risk
    corr_score = _clamp(avg_corr * 70)
    # High portfolio correlation → concentrated risk
    port_corr_score = _clamp(port_corr * 60)
    # Down-market correlation penalized more
    down_corr_score = _clamp(down_corr * 80)
    # HHI: 1/n → perfectly diversified, 1.0 → fully concentrated
    hhi_score = _clamp(hhi * 100)

    raw = (
        corr_score * 0.25
        + port_corr_score * 0.20
        + down_corr_score * 0.30
        + hhi_score * 0.25
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
    """Score based on performance during rate spikes and VIX crises."""
    details = {}

    if returns is None or len(returns) < 30:
        return {"score": 50, "rating": "yellow", "details": {"data_available": False}}

    # Rate spike regime: days when TNX rises >2 std devs
    if tnx_changes is not None and len(tnx_changes) > 30:
        aligned = pd.DataFrame({"stock": returns, "tnx": tnx_changes}).dropna()
        tnx_std = aligned["tnx"].std()
        if tnx_std > 0:
            spike_days = aligned[aligned["tnx"] > 2 * tnx_std]
            if len(spike_days) > 3:
                rate_spike_return = spike_days["stock"].mean() * 100
            else:
                rate_spike_return = 0
        else:
            rate_spike_return = 0
    else:
        rate_spike_return = 0
    details["avg_return_rate_spike_pct"] = round(rate_spike_return, 3)

    # VIX crisis regime: days when VIX > 25
    if vix_series is not None and len(vix_series) > 0:
        aligned = pd.DataFrame({"stock": returns, "vix": vix_series}).dropna()
        crisis_days = aligned[aligned["vix"] > 25]
        if len(crisis_days) > 3:
            vix_crisis_return = crisis_days["stock"].mean() * 100
        else:
            vix_crisis_return = 0
    else:
        vix_crisis_return = 0
    details["avg_return_vix_crisis_pct"] = round(vix_crisis_return, 3)

    # Score: negative returns during stress → higher score
    # -0.5% per day during crisis → moderate, -2%+ → very bad
    rate_score = _clamp(abs(min(rate_spike_return, 0)) * 30)
    vix_score = _clamp(abs(min(vix_crisis_return, 0)) * 30)

    raw = rate_score * 0.50 + vix_score * 0.50
    score = _clamp(round(raw))
    return {"score": score, "rating": _rating(score), "details": details}


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
