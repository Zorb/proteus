"""
actions.py — Entry-aware action layer: partial take-profit / cut-loss suggestions.

Deterministic and stateless. Fixed P&L ladders (configurable via .env) pick a
base tier, then the position's risk rating shifts the suggestion one tier:
red acts harder, green softer, yellow keeps the base. The risk score itself
stays forward-looking — this module is the only place entry price matters.

Suggestions are recomputed each run against the current holding; after a real
trim the position shrinks but avg price doesn't, so a standing suggestion
simply repeats at the twice-weekly cadence.
"""

import math

# Default escalation caps when red risk pushes past the top ladder tier
# (overridable via ACTION_RED_TP_CAP / ACTION_RED_CL_CAP, see config.py)
RED_TOP_TP_TRIM = 75.0
RED_TOP_CL_TRIM = 100.0


def _is_missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def unrealized_pnl_pct(current_price, avg_price):
    """Percent gain/loss from entry, or None when either side is unusable.

    None (not 0) when entry is missing or the live price failed to fetch —
    downstream fallbacks substitute avg_price for a missing live price, which
    would otherwise fake a flat 0%. Missing values arrive as None (IBKR path)
    or NaN (blank CSV cells via pandas), so both are treated as absent.
    """
    if _is_missing(current_price) or _is_missing(avg_price):
        return None
    if avg_price <= 0 or current_price <= 0:
        return None
    return (current_price - avg_price) / avg_price * 100


def parse_ladder(spec, kind):
    """Parse a ladder spec like "25:25,50:50" into [(threshold, trim_pct), ...].

    kind is "gain" (thresholds must be > 0) or "loss" (must be < 0). Tiers are
    sorted by absolute threshold ascending, so index order equals escalation
    order for both ladders.
    """
    if kind not in ("gain", "loss"):
        raise ValueError(f"kind must be 'gain' or 'loss', got {kind!r}")
    tiers = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(":")
        if len(pieces) != 2:
            raise ValueError(
                f"Malformed ladder tier {part!r} in {spec!r} (want 'threshold:trim_pct')"
            )
        try:
            threshold, trim = float(pieces[0]), float(pieces[1])
        except ValueError:
            raise ValueError(
                f"Non-numeric ladder tier {part!r} in {spec!r} (want 'threshold:trim_pct')"
            )
        if kind == "gain" and threshold <= 0:
            raise ValueError(f"Gain ladder thresholds must be > 0, got {threshold}")
        if kind == "loss" and threshold >= 0:
            raise ValueError(f"Loss ladder thresholds must be < 0, got {threshold}")
        if not 0 < trim <= 100:
            raise ValueError(f"Trim must be in (0, 100], got {trim} in {spec!r}")
        tiers.append((threshold, trim))
    if not tiers:
        raise ValueError(f"Empty ladder spec {spec!r}")
    tiers.sort(key=lambda t: abs(t[0]))
    return tiers


def _crossed_index(pnl_pct, ladder, kind):
    """Highest ladder index whose threshold the P&L has crossed, or None."""
    idx = None
    for i, (threshold, _) in enumerate(ladder):
        crossed = pnl_pct >= threshold if kind == "gain" else pnl_pct <= threshold
        if crossed:
            idx = i
    return idx


def evaluate_position_action(
    pnl_pct,
    rating,
    tp_ladder,
    cl_ladder,
    red_tp_cap=RED_TOP_TP_TRIM,
    red_cl_cap=RED_TOP_CL_TRIM,
):
    """Suggest a partial exit for one position.

    Returns {"action", "trim_pct", "reason"}. Actions: take_profit_candidate /
    cut_loss_candidate (with trim_pct of the current position), hold, no_data.
    red_tp_cap / red_cl_cap bound the red-risk escalation past the top tier.
    """
    if pnl_pct is None:
        return {
            "action": "no_data",
            "trim_pct": 0,
            "reason": "entry price or current price unavailable",
        }

    tp_idx = _crossed_index(pnl_pct, tp_ladder, "gain")
    cl_idx = _crossed_index(pnl_pct, cl_ladder, "loss")

    if tp_idx is None and cl_idx is None:
        return {
            "action": "hold",
            "trim_pct": 0,
            "reason": f"{pnl_pct:+.1f}% from entry, inside ladder thresholds",
        }

    if tp_idx is not None:
        ladder, idx, action, kind = tp_ladder, tp_idx, "take_profit_candidate", "gain"
    else:
        ladder, idx, action, kind = cl_ladder, cl_idx, "cut_loss_candidate", "loss"

    threshold, base_trim = ladder[idx]
    base = f"{pnl_pct:+.1f}% from entry crossed the {threshold:+g}% tier (base trim {base_trim:g}%)"

    if rating == "red":
        if idx + 1 < len(ladder):
            trim = ladder[idx + 1][1]
        else:
            cap = red_cl_cap if kind == "loss" else red_tp_cap
            trim = max(base_trim, cap)
        reason = f"{base}; red risk escalates to {trim:g}%"
    elif rating == "green":
        if idx == 0:
            return {
                "action": "hold",
                "trim_pct": 0,
                "reason": f"{base}; green risk softens to hold — watch this one",
            }
        trim = ladder[idx - 1][1]
        reason = f"{base}; green risk softens to {trim:g}%"
    else:
        trim = base_trim
        reason = f"{base}; {rating} risk keeps base tier"

    return {"action": action, "trim_pct": trim, "reason": reason}


def action_label(action, trim_pct, position_size=None):
    """Human-readable label for an action — the single rendering source shared
    by every surface (email table, and any future console/Telegram view).

    With position_size, sell suggestions carry the share count in parentheses,
    e.g. "Trim 25% (30)" for a 120-share position.
    """
    if action == "take_profit_candidate":
        label = f"Trim {trim_pct:g}%"
    elif action == "cut_loss_candidate":
        label = "Exit" if trim_pct >= 100 else f"Cut {trim_pct:g}%"
    else:
        return "–"
    if not _is_missing(position_size) and position_size > 0:
        shares = round(position_size * trim_pct / 100, 1)
        label += f" ({shares:g})"
    return label
