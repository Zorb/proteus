# Proteus

AI-powered stock portfolio risk monitor. Runs on a schedule, scores your holdings across 7 risk categories, suggests partial take-profit/cut-loss actions from your entry prices, enriches with recent news headlines, and delivers a plain-language briefing via email or Telegram.

Configured for a **long-only UK ISA holding USD-denominated stocks**. Values and weights cover the stock holdings, excluding cash. Unexpected currencies and short positions are rejected; legacy CSVs without a `Currency` column are interpreted as USD. This is a decision aid for which holdings to keep, review, or trim; the risk score is a historical heuristic, not a forecast of returns.

## How it works

1. Loads your portfolio from Interactive Brokers (or a CSV fallback)
2. Fetches price history, fundamentals, and news from Yahoo Finance
3. Scores each stock 0-100 across 7 risk categories (drawdown, volatility, liquidity, balance sheet, correlation, concentration, regime sensitivity)
4. Computes partial take-profit / cut-loss suggestions from entry price
5. Sends everything to Claude (Opus 4.6 + extended thinking) for interpretation
6. Delivers a concise report via email, Telegram, or both

## Setup

### 1. Environment variables

Create a `.env` file:

```env
# Required
ANTHROPIC_KEY=your_key

# Email (default notification method)
NOTIFICATION_METHOD=email
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your_app_password
EMAIL_FROM=you@gmail.com
EMAIL_TO=you@gmail.com

# Optional: Telegram (set NOTIFICATION_METHOD=telegram or both)
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional: Interactive Brokers portfolio sync
IBKR_FLEX_TOKEN=your_token
IBKR_FLEX_QUERY_ID=your_query_id

# Optional: take-profit / cut-loss ladders ("pnl_threshold:trim_pct,...")
ACTION_TP_LADDER=25:25,50:50
ACTION_CL_LADDER=-8:50,-15:100
# Red-risk escalation caps past the top tier
ACTION_RED_TP_CAP=75
ACTION_RED_CL_CAP=100
```

### 2. Portfolio

Edit `portfolio.csv` with your holdings (or copy from `portfolio.example.csv`):

```csv
Ticker,Position_Size,Avg_Price
AAPL,50,150.00
MSFT,30,280.00
```

To sync automatically from Interactive Brokers instead, see [ibkr-setup.txt](ibkr-setup.txt) for Flex Query configuration instructions.

### 3. Run

**Docker (recommended):**
```bash
docker-compose up -d --build
```

**Local / systemd:**
```bash
python stock_risk_agent.py          # one-shot: run once and exit (systemd timer mode)
python stock_risk_agent.py --loop   # internal scheduler: run now, then daily at SCHEDULE_TIME
```

The Docker image uses `--loop` (daily at `SCHEDULE_TIME`, default 08:00). The systemd deployment (`proteus.service` + `proteus.timer`) uses one-shot mode on the timer's schedule (Mon+Fri 10:00 UTC, chosen to avoid IBKR's early-morning-ET maintenance window); a failed one-shot run exits nonzero so the unit shows as failed. The IBKR fetch retries 3× before falling back to `portfolio.csv`, which is refreshed after a validated sync, including a valid empty portfolio. Invalid/unsupported holdings fail explicitly rather than reusing old positions. Fallback reports are flagged in the subject line and report header.

An AI outage sends a deterministic quantitative briefing, then marks the one-shot run failed. A missing valuation or entirely unavailable holding withholds portfolio scores and candidates and attempts a failure notice. Notification errors propagate; with `both`, both channels are attempted and either failure marks the run failed. No live orders are placed.

### 4. Tests

```bash
pip install pytest
python -m pytest tests/
```

## Risk categories

| Category | Weight | What it measures |
|---|---|---|
| Drawdown | 20% | Max drawdown, distance from 5y high, Ulcer Index |
| Downside Volatility | 20% | Sortino ratio, CVaR 5%, down-market beta |
| Correlation | 15% | SPY correlation, sector concentration |
| Balance Sheet | 15% | Debt/EBITDA, current ratio, interest coverage |
| Liquidity | 10% | Days to liquidate, volume trends |
| Concentration | 10% | Position size risk |
| Regime Sensitivity | 10% | Performance during rate spikes and VIX crises |

Each scored 0-100. Green is below 35, yellow is 35 to below 65, and red is 65 or higher. A red alert triggers if a position of at least 5% of the portfolio (configurable via `ALERT_MIN_POSITION_PCT`) has a category above 80 or goes red overall, or if the portfolio composite is at least 65. Smaller positions can have critical alerts without flagging the whole portfolio. Weights retain full precision for calculations and alert thresholds.

Missing drawdown history is unknown risk, with a neutral minimum score of 50 and explicit availability flags. Missing categories, short lookbacks, and incomplete portfolio history mark the report provisional. Empty/corrupt history is retried; history older than seven calendar days is rejected. Prices use a valid quote or the latest validated close, never entry cost. Portfolio return metrics require a complete common price calendar; gaps make those metrics unavailable rather than deleting market moves or renormalizing away a holding. These metrics are a current-weight, daily-rebalanced historical proxy, not the account's realized performance.

## Action suggestions

Separately from the historical risk score, each position gets a deterministic partial take-profit / cut-loss suggestion based on unrealized P&L from entry. Fixed ladders pick a base tier — `ACTION_TP_LADDER` (default `25:25,50:50`: at +25% gain trim 25% of the position, at +50% trim 50%) and `ACTION_CL_LADDER` (default `-8:50,-15:100`: at −8% cut half, at −15% exit) — then the risk rating shifts the suggestion one tier: red escalates, green softens, yellow keeps the base. A critical category alert takes precedence over a green composite; incomplete risk evidence disables green softening. Inside the P&L thresholds, a critical alert or incomplete evidence produces `Review` rather than `hold`. Missing entry data produces no sized candidate.

Suggestions are candidates, not orders; Claude addresses each one in the report. `hold` means no ladder action, not a validated endorsement of the investment thesis. Suggestions remain stateless and can repeat even after a partial trim if the P&L tier still applies.

## Data sources

- **Yahoo Finance** — price history, fundamentals, news headlines (RSS feed, no key)
- **Interactive Brokers** — live portfolio positions via Flex Query (optional)
