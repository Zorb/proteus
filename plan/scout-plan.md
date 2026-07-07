# Proteus Scout — plan for the candidate-screener agent and notifier extraction

**Goal:** a second agent in the same repo that scans a candidate universe weekly, scores it
with the existing `risk_framework`, ranks candidates by what the current portfolio actually
lacks, and emails a short "worth researching" report — without touching the daily risk
monitor's reliability.

**Non-goals:** trade execution, buy/sell recommendations presented as advice, real-time
screening, intraday data.

**Status:** not started. Decision 2026-07-07: build in this repo (not a new project) — Scout
reuses `risk_framework`, `portfolio_sync`, `sentiment`, `_retry`, and the same `.env`.

**Deployment note (2026-07-07):** the repo's reference deployment is Docker Compose, but
ubuntu-main runs via systemd instead (`proteus.service` + `proteus.timer`, weekdays 08:00 UTC;
`stock_risk_agent.py` with no args is one-shot, `--loop` runs the internal scheduler). For
Phase 3 on this host, add `proteus-scout.service` + `proteus-scout.timer`
(`OnCalendar=Sun 17:00`) instead of a second compose service — `SCOUT_SCHEDULE` /
`SCOUT_RUN_ON_START` are then unnecessary. Scout's entrypoint should follow the same
one-shot-by-default convention.

---

## Architecture

```
proteus/
├── stock_risk_agent.py      # existing daily monitor (unchanged behavior)
├── scout.py                 # NEW weekly screener entrypoint
├── notify.py                # NEW shared notification module (extracted in Phase 0)
├── universe.py              # NEW candidate universe + data cache
├── risk_framework.py        # shared, unchanged — the reason both agents live in one repo
├── portfolio_sync.py        # shared (scout needs current holdings for complement ranking)
├── sentiment.py             # shared (optional enrichment for finalists)
├── config.py                # shared + new SCOUT_* / notification settings
├── data/                    # NEW gitignored cache dir (parquet), docker volume
├── universe.csv             # NEW committed candidate ticker list
└── tests/
```

Two docker-compose services sharing one image, different `command:`. A scout crash can
never take down the morning risk report; the scout's heavy data pulls never slow the
daily run.

---

## Phase 0 — Extract `notify.py` (do this first; improves Proteus even if Scout never ships)

**~30–60 min.** Currently `send_report`, `send_email`, `send_telegram`, and
`format_html_report` are methods on `StockRiskAgent`, so nothing else can reuse them.

1. Create `notify.py` with module-level functions:
   - `send_report(report_text, *, subject, title, summary_table_html=None)` — routes on
     `config.NOTIFICATION_METHOD` exactly as today.
   - `send_email(...)`, `send_telegram(...)`, `format_html_report(...)` moved verbatim,
     then de-hardcode the two strings that block reuse: the email subject
     (`Proteus Risk Report - {date}`) and the HTML header title (`Proteus Risk Report`)
     become parameters with those values as defaults.
2. The risk-table HTML builder is risk-report-specific — keep it in `stock_risk_agent.py`
   and pass the rendered table into `notify` as an optional block (`summary_table_html`),
   so `notify.py` stays generic.
3. `StockRiskAgent` methods become one-line delegations (or call `notify` directly from
   `analyze_portfolio` and delete the methods).
4. Tests: `format_html_report` is pure — add tests for the header regex, ticker-line
   regex, numbered-action detection, and HTML escaping of `<`/`&` in report text.

**Acceptance:** daily agent behavior unchanged (same email, byte-identical HTML for the
default subject/title); `python -c "import notify"` works without importing the agent.

---

## Phase 1 — Candidate universe and data cache

**The rate-limit phase — get this right before any scoring.** ~500 tickers × daily
Yahoo calls is how you get IP-throttled; the answer is a slow, resumable, cached crawl.

1. `universe.csv` (committed): start with S&P 500 constituents + a hand-picked list of
   ETFs / non-US names you care about. A static list refreshed manually every few months
   is fine — do not scrape it at runtime. Optional `watchlist.csv` (gitignored) merged in.
2. `universe.py` cache layer:
   - `get_candidate_data(ticker)` → dict of fundamentals + 1y history, reading from a
     parquet/JSON cache under `data/` with a freshness window (`SCOUT_CACHE_DAYS`, default 7).
   - `refresh_cache(tickers)` — fetch only stale entries, in batches of ~20 with a few
     seconds' sleep between batches, resumable (a crash mid-crawl loses nothing; rerun
     continues). Reuse the `_retry` helper.
   - Store per-ticker: sector, market cap, trailing P/E, avg volume, 1y closes,
     balance-sheet fields used by `score_balance_sheet`.
3. Docker: add `./data:/app/data` volume to both services; add `data/` to `.gitignore`
   and `.dockerignore`.

**Acceptance:** `refresh_cache` over the full universe completes across ≤2 runs without
rate-limit errors; a second invocation the same day does zero network calls.

---

## Phase 2 — Scoring and complement ranking

Pure functions, heavily testable — this is where the existing framework pays off.

1. Score every cached candidate with the existing functions: `score_drawdown_profile`
   (1y history is enough — pass it for all three windows or fetch 3y/5y lazily for
   finalists only), `score_downside_volatility`, `score_balance_sheet`, and
   `score_liquidity_risk` with a hypothetical position (e.g. 5% of current portfolio value).
2. Hard filters first (cheap): drop existing holdings, drop anything red on balance
   sheet or liquidity, minimum market cap / volume floor.
3. Complement score vs the *current* portfolio (this is the differentiator):
   - **Correlation fit:** correlation of candidate 1y returns vs portfolio returns
     (already normalized/aligned thanks to `normalize_daily_index`) — lower is better.
   - **Sector fit:** bonus for sectors where `sector_weights` is underweight, penalty
     for the dominant sector.
   - **Quality:** blend of the candidate's own risk scores (lower composite = better).
   - Weighted rank, e.g. `0.4·correlation_fit + 0.3·sector_fit + 0.3·quality` — put the
     weights in one dict like `WEIGHTS` so they're tunable and testable.
4. Take top `SCOUT_TOP_N` (default 5).

**Acceptance:** unit tests with synthetic data prove: an anti-correlated candidate
outranks a correlated one of equal quality; a candidate in the overweight sector is
penalized; existing holdings never appear.

---

## Phase 3 — The scout agent itself

1. `scout.py`: load portfolio (same IBKR/CSV path as the monitor) → build portfolio
   context (sector weights, portfolio returns) → `refresh_cache` → rank → fetch full
   detail + optional Alpha Vantage sentiment for the finalists only → Claude → `notify`.
2. New `SCOUT_PROMPT` in `config.py`: same voice and rules as `RISK_PROMPT` (cite numbers,
   no fabrication, plain text), framing: "candidates worth researching, not
   recommendations"; per candidate: why it complements the portfolio (the correlation and
   sector numbers), the risk scores, and what would need checking before buying.
3. Scheduling: weekly (`SCOUT_SCHEDULE`, e.g. `sunday 17:00`), run-once-on-start optional
   via `SCOUT_RUN_ON_START` to keep container startups cheap.
4. `docker-compose.yml`: second service `scout` with `command: python scout.py`, same
   image, same `.env`, shared `data/` volume.
5. Delivery via `notify.send_report(..., subject="Proteus Scout Report - {date}",
   title="Proteus Scout Report")` — its own weekly email, not crowding the daily one.

**Acceptance:** `docker-compose up` runs both services; killing scout doesn't affect the
monitor; the weekly email arrives with N candidates, each citing real cached numbers.

---

## Phase 4 — Polish (optional, later)

- Track suggested-before candidates (small JSON state file) so the report says "new this
  week" vs "still ranked".
- Polymarket earnings odds for finalists.
- A `--once` CLI flag on both agents for manual runs.
- CI: GitHub Action running `pytest` on PRs (the repo has none today).

---

## New config (all env-overridable, following existing patterns)

| Variable | Default | Purpose |
|---|---|---|
| `SCOUT_SCHEDULE` | `sunday 17:00` | weekly run time |
| `SCOUT_TOP_N` | `5` | candidates in the report |
| `SCOUT_CACHE_DAYS` | `7` | cache freshness window |
| `SCOUT_MIN_MARKET_CAP` | `2e9` | hard filter floor |
| `SCOUT_RUN_ON_START` | `false` | run immediately on container start |

## Risks / constraints to respect

- **Yahoo rate limits** are the main failure mode — everything flows through the Phase 1
  cache; never fetch the universe inside the report path.
- **Prompt size:** only finalists go to Claude with full detail; the universe never does.
- **Alpha Vantage free tier is 25 req/day** — finalists only, and it already batches.
- **Framing:** the report must stay "research starting points with numbers", consistent
  with the no-fabrication rule — the model only sees cached, real data.

## Suggested commit sequence

1. `refactor: extract notify.py` (Phase 0 — standalone win, merge immediately)
2. `feat: candidate universe and cached data layer` (Phase 1)
3. `feat: candidate scoring and complement ranking` (Phase 2)
4. `feat: scout agent, weekly schedule, compose service` (Phase 3)
