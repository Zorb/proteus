# Proteus: remove Polymarket + entry-aware action layer

**Status: IMPLEMENTED 2026-07-18.** Verified: 65/65 tests pass (17 new in
tests/test_actions.py), zero polymarket code references, live systemd run
succeeded — action flags computed for all 13 positions, email sent.

## Context

Proteus scores portfolio risk with 7 forward-looking factors but ignores entry price
entirely — IBKR's `costBasisPrice` is parsed into `Avg_Price` (`portfolio_sync.py:210-227`),
attached to each position (`stock_risk_agent.py:239-240`), then used only as a
missing-price fallback (`stock_risk_agent.py:258`). Polymarket is already dead code:
the live fetch is commented out and the prompt receives a hardcoded
"Polymarket data unavailable." string.

Agreed design (discussed 2026-07-18):
- **Remove Polymarket** entirely.
- **Entry awareness as a separate deterministic action layer** — the risk score stays
  forward-looking; a new layer combines unrealized P&L% with the risk context to flag
  per-position actions.
- **Flagged candidates with PARTIAL sizing** (updated per user feedback): suggestions
  are per-position trims ("trim 25%", "cut 50%", "exit"), not all-or-nothing.
- **Ladder + risk modifier, thresholds via .env**: fixed P&L tiers set the base
  suggestion; the position's risk rating shifts it one tier (red = act harder,
  green = softer).

## Change 1 — Remove Polymarket

- `sentiment.py`: delete `GAMMA_API_BASE` (:14), `fetch_polymarket_earnings` (:18-43),
  `_search_earnings_markets` (:46-130), `format_polymarket_data` (:133-168); fix module
  docstring (:4). Alpha Vantage functions (:176-323) stay.
- `stock_risk_agent.py`: delete the Polymarket block (:445-450) but KEEP
  `tickers = ...` (:446, needed by Alpha Vantage call at :455); delete the
  `polymarket_data=polymarket_str` kwarg (:481). Placeholder and kwarg must go together.
- `config.py`: drop "prediction market odds" from the intro (:44), the
  `PREDICTION MARKET DATA (Polymarket)` section (:55-56), and the Polymarket weighting
  line (:67).
- Docs: `README.md:10`, `README.md:95`; strike the Polymarket future-enhancement line
  in `plan/scout-plan.md:147` (note "removed 2026-07-18" so it doesn't come back).

## Change 2 — Action layer (new `actions.py`)

New small pure module `actions.py` (keeps `risk_framework.py`'s meaning intact;
no I/O, fully testable):

- `unrealized_pnl_pct(current_price, avg_price) -> float | None` — None when
  `avg_price` missing/<=0 or `current_price` is None (e.g. CSV rows without entry,
  tickers whose live fetch failed — do NOT compute a fake 0% from the fallback price).
- `parse_ladder(spec) -> list[(threshold, trim_pct)]` — parses compact .env specs like
  `"25:25,50:50"`; validated, sorted, clear error on malformed input.
- `evaluate_position_action(pnl_pct, rating, tp_ladder, cl_ladder)
  -> {"action": ..., "trim_pct": ..., "reason": ...}` — deterministic two-step:
  1. **Base tier from the ladder.** Take-profit default `25:25,50:50` (at +25% gain
     suggest trimming 25% of the current position; at +50%, 50%). Cut-loss default
     `-8:50,-15:100` (at −8% cut half; at −15% exit). Highest tier crossed wins.
  2. **Risk modifier shifts one tier.** `red` → one tier harder (tier 1 → tier 2's
     trim; already at top → 75% for gains, exit stays exit for losses). `green` → one
     tier softer (tier 1 → downgraded to `hold`, with the near-miss noted in the
     reason). `yellow` → base tier unchanged.
  Actions: `take_profit_candidate` / `cut_loss_candidate` (both with `trim_pct`),
  `hold` (`trim_pct` 0), `no_data`. The reason string cites P&L, tier, rating and any
  modifier applied (e.g. "+31% from entry ≥ +25% tier; risk 27 (green) softens
  trim-25% → hold").
  **Stateless by design:** suggestions are recomputed each run against the *current*
  holding; after a real trim, position size shrinks but avg_price doesn't, so a
  standing suggestion simply repeats at twice-weekly cadence. Tracking acted-on tiers
  is a noted future enhancement, not v1.

Wiring:
- `config.py`: `ACTION_TP_LADDER` (default `"25:25,50:50"`), `ACTION_CL_LADDER`
  (default `"-8:50,-15:100"`) via `os.getenv`, next to `ALERT_MIN_POSITION_PCT`
  (:35-37). Document both in `.env` (values) and `README.md`.
- `stock_risk_agent.py`: in the per-position loop, compute pnl + action once; add
  `avg_price`, `unrealized_pnl_pct`, `action`, `trim_pct`, `action_reason` to each
  stock's entry in `portfolio_summary` (:416-424) and to `display_data` (:429-443) so
  both the prompt and the HTML table see them.

## Change 3 — Prompt + report

- `config.py` RISK_PROMPT: mention entry price/unrealized P&L in the intro (:44);
  per-ticker block spec (:90-94) → require stating P&L% from entry and explicitly
  addressing any sized suggestion (endorse "trim 25%" or push back, with a cited
  number); WHAT TO DO (:96-99) → list flagged candidates with their trim sizes and
  rationale. Add one scoring-reference line: suggestions are deterministic
  ladder-tier candidates, not orders — Claude's job is judgment on top.
- `stock_risk_agent.py` `format_html_report`: add **Entry / P&L% / Action** columns to
  the per-stock risk table (rows :608-615, header :623-628); P&L cell colored
  green/red; action cell shows the sized suggestion ("Trim 25%" / "Cut 50%" / "Exit",
  – for hold/no_data).

## Tests

New `tests/test_actions.py` (first tests outside risk_framework, mirroring its style):
ladder parsing (valid, malformed, unsorted specs); tier boundaries at exactly −8/−15
and +25/+50; highest-tier-wins; risk modifier both directions (red escalation at each
tier incl. top-tier 75%/exit caps, green softening incl. tier-1 → hold downgrade,
yellow no-op); `no_data` on missing entry or price; reason-string content. Existing
`tests/test_risk_framework.py` untouched (no Polymarket references exist).

## Verification

1. `.venv/bin/python -m pytest tests/ -q` — all green.
2. Grep repo for `polymarket` — zero code hits.
3. Live end-to-end: `sudo -n systemctl start proteus.service`, watch journal —
   no "Polymarket skipped" line; email report shows Entry/P&L/Action columns and
   Claude addressing flags (current portfolio should produce real candidates).

## Housekeeping

- Copy this plan to `/home/or1on/proteus/plan/entry-aware-actions.md` (repo convention,
  cf. `plan/scout-plan.md`); mark implemented when verified.
- Leave changes uncommitted unless asked (repo: Zorb/proteus).
