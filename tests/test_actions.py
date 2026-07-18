"""Unit tests for actions.py — entry-aware partial take-profit / cut-loss layer."""

import pytest

import actions


TP = actions.parse_ladder("25:25,50:50", kind="gain")
CL = actions.parse_ladder("-8:50,-15:100", kind="loss")


def evaluate(pnl, rating="yellow"):
    return actions.evaluate_position_action(pnl, rating, TP, CL)


# ---------------------------------------------------------------------------
# unrealized_pnl_pct
# ---------------------------------------------------------------------------


def test_pnl_basic_gain_and_loss():
    assert actions.unrealized_pnl_pct(130.0, 100.0) == pytest.approx(30.0)
    assert actions.unrealized_pnl_pct(90.0, 100.0) == pytest.approx(-10.0)


def test_pnl_none_when_data_missing():
    assert actions.unrealized_pnl_pct(None, 100.0) is None
    assert actions.unrealized_pnl_pct(100.0, None) is None
    assert actions.unrealized_pnl_pct(100.0, 0) is None
    assert actions.unrealized_pnl_pct(100.0, -5.0) is None
    assert actions.unrealized_pnl_pct(0, 100.0) is None


def test_pnl_none_on_nan():
    # pandas represents blank CSV cells as NaN, which is neither None nor <= 0
    nan = float("nan")
    assert actions.unrealized_pnl_pct(nan, 100.0) is None
    assert actions.unrealized_pnl_pct(100.0, nan) is None
    assert actions.unrealized_pnl_pct(nan, nan) is None


# ---------------------------------------------------------------------------
# parse_ladder
# ---------------------------------------------------------------------------


def test_parse_ladder_valid():
    assert actions.parse_ladder("25:25,50:50", kind="gain") == [
        (25.0, 25.0),
        (50.0, 50.0),
    ]
    assert actions.parse_ladder("-8:50,-15:100", kind="loss") == [
        (-8.0, 50.0),
        (-15.0, 100.0),
    ]


def test_parse_ladder_sorts_by_abs_threshold():
    assert actions.parse_ladder("50:50,25:25", kind="gain") == [
        (25.0, 25.0),
        (50.0, 50.0),
    ]
    assert actions.parse_ladder("-15:100,-8:50", kind="loss") == [
        (-8.0, 50.0),
        (-15.0, 100.0),
    ]


@pytest.mark.parametrize(
    "spec,kind",
    [
        ("", "gain"),
        ("25", "gain"),
        ("25:25:25", "gain"),
        ("abc:10", "gain"),
        ("25:0", "gain"),  # trim must be > 0
        ("25:101", "gain"),  # trim must be <= 100
        ("-8:50", "gain"),  # gain thresholds must be positive
        ("8:50", "loss"),  # loss thresholds must be negative
    ],
)
def test_parse_ladder_rejects_malformed(spec, kind):
    with pytest.raises(ValueError):
        actions.parse_ladder(spec, kind=kind)


def test_parse_ladder_rejects_bad_kind():
    with pytest.raises(ValueError):
        actions.parse_ladder("25:25", kind="sideways")


# ---------------------------------------------------------------------------
# evaluate_position_action — base tiers (yellow = no modifier)
# ---------------------------------------------------------------------------


def test_no_data():
    result = evaluate(None)
    assert result["action"] == "no_data"
    assert result["trim_pct"] == 0


def test_hold_inside_thresholds():
    for pnl in (-7.9, 0.0, 24.9):
        result = evaluate(pnl)
        assert result["action"] == "hold"
        assert result["trim_pct"] == 0


def test_tp_tier_boundaries():
    assert evaluate(25.0)["trim_pct"] == 25.0  # exactly at tier 1
    assert evaluate(49.9)["trim_pct"] == 25.0
    assert evaluate(50.0)["trim_pct"] == 50.0  # exactly at tier 2
    assert evaluate(120.0)["trim_pct"] == 50.0  # highest tier wins
    assert evaluate(25.0)["action"] == "take_profit_candidate"


def test_cl_tier_boundaries():
    assert evaluate(-8.0)["trim_pct"] == 50.0  # exactly at tier 1
    assert evaluate(-14.9)["trim_pct"] == 50.0
    assert evaluate(-15.0)["trim_pct"] == 100.0  # exactly at tier 2
    assert evaluate(-60.0)["trim_pct"] == 100.0  # highest tier wins
    assert evaluate(-8.0)["action"] == "cut_loss_candidate"


# ---------------------------------------------------------------------------
# evaluate_position_action — risk modifier
# ---------------------------------------------------------------------------


def test_red_escalates_one_tier():
    assert evaluate(30.0, "red")["trim_pct"] == 50.0  # tier 1 -> tier 2
    assert evaluate(-10.0, "red")["trim_pct"] == 100.0  # cut 50 -> exit


def test_red_at_top_tier_caps():
    result = evaluate(60.0, "red")
    assert result["trim_pct"] == actions.RED_TOP_TP_TRIM  # gains cap at 75
    assert evaluate(-20.0, "red")["trim_pct"] == 100.0  # exit stays exit


def test_green_softens_one_tier():
    assert evaluate(60.0, "green")["trim_pct"] == 25.0  # tier 2 -> tier 1
    assert evaluate(-16.0, "green")["trim_pct"] == 50.0  # exit -> cut 50


def test_green_tier1_downgrades_to_hold():
    for pnl in (30.0, -10.0):
        result = evaluate(pnl, "green")
        assert result["action"] == "hold"
        assert result["trim_pct"] == 0
        assert "green" in result["reason"]  # near-miss is explained


def test_yellow_keeps_base_tier():
    assert evaluate(30.0, "yellow")["trim_pct"] == 25.0
    assert evaluate(-10.0, "yellow")["trim_pct"] == 50.0


def test_custom_escalation_caps():
    result = actions.evaluate_position_action(
        60.0, "red", TP, CL, red_tp_cap=90.0, red_cl_cap=100.0
    )
    assert result["trim_pct"] == 90.0
    # partial cut-loss ladder with a non-exit cap stays partial under red
    partial_cl = actions.parse_ladder("-8:25,-15:60", kind="loss")
    result = actions.evaluate_position_action(
        -20.0, "red", TP, partial_cl, red_cl_cap=60.0
    )
    assert result["trim_pct"] == 60.0


# ---------------------------------------------------------------------------
# action_label
# ---------------------------------------------------------------------------


def test_action_label():
    assert actions.action_label("take_profit_candidate", 25.0) == "Trim 25%"
    assert actions.action_label("cut_loss_candidate", 50.0) == "Cut 50%"
    assert actions.action_label("cut_loss_candidate", 100.0) == "Exit"
    assert actions.action_label("hold", 0) == "–"
    assert actions.action_label("no_data", 0) == "–"


def test_action_label_with_share_count():
    assert actions.action_label("take_profit_candidate", 25.0, 120) == "Trim 25% (30)"
    assert actions.action_label("cut_loss_candidate", 50.0, 630) == "Cut 50% (315)"
    assert actions.action_label("cut_loss_candidate", 100.0, 630) == "Exit (630)"
    # fractional positions keep one decimal
    assert actions.action_label("take_profit_candidate", 25.0, 30) == "Trim 25% (7.5)"
    # missing/invalid sizes omit the parenthetical; hold never gets one
    assert actions.action_label("take_profit_candidate", 25.0, None) == "Trim 25%"
    assert actions.action_label("take_profit_candidate", 25.0, float("nan")) == "Trim 25%"
    assert actions.action_label("take_profit_candidate", 25.0, 0) == "Trim 25%"
    assert actions.action_label("hold", 0, 100) == "–"


# ---------------------------------------------------------------------------
# reason strings
# ---------------------------------------------------------------------------


def test_reason_cites_pnl_tier_and_rating():
    result = evaluate(31.4, "red")
    assert "+31.4%" in result["reason"]
    assert "+25%" in result["reason"]
    assert "red" in result["reason"]

    result = evaluate(-9.2, "yellow")
    assert "-9.2%" in result["reason"]
    assert "-8%" in result["reason"]
