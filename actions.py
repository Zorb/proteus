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

# Escalation cap when red risk pushes past the top take-profit tier
RED_TOP_TP_TRIM = 75.0


def unrealized_pnl_pct(current_price, avg_price):
    """Percent gain/loss from entry, or None when either side is unusable.

    None (not 0) when entry is missing or the live price failed to fetch —
    downstream fallbacks substitute avg_price for a missing live price, which
    would otherwise fake a flat 0%.
    """
    if current_price is None or avg_price is None:
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


def evaluate_position_action(pnl_pct, rating, tp_ladder, cl_ladder):
    """Suggest a partial exit for one position.

    Returns {"action", "trim_pct", "reason"}. Actions: take_profit_candidate /
    cut_loss_candidate (with trim_pct of the current position), hold, no_data.
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
            trim = 100.0 if kind == "loss" else max(base_trim, RED_TOP_TP_TRIM)
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
