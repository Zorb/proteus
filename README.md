# Proteus

AI-powered stock portfolio risk monitor. Runs on a schedule, scores your holdings across 7 risk categories, suggests partial take-profit/cut-loss actions from your entry prices, enriches with news sentiment, and delivers a plain-language briefing via email or Telegram.

## How it works

1. Loads your portfolio from Interactive Brokers (or a CSV fallback)
2. Fetches price history, fundamentals, and news from Yahoo Finance
3. Scores each stock 0-100 across 7 risk categories (drawdown, volatility, liquidity, balance sheet, correlation, concentration, regime sensitivity)
4. Computes partial take-profit / cut-loss suggestions from entry price and pulls Alpha Vantage news sentiment
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

# Optional: Alpha Vantage news sentiment (free key, 25 req/day)
ALPHAVANTAGE_API_KEY=your_key

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

The Docker image uses `--loop` (daily at `SCHEDULE_TIME`, default 08:00). The systemd deployment (`proteus.service` + `proteus.timer`) uses one-shot mode on the timer's schedule (Mon+Fri 08:00); a failed one-shot run exits nonzero so the unit shows as failed.

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

Each scored 0-100. Green (0-35), Yellow (35-65), Red (65-100). A red alert triggers if a position of at least 5% of the portfolio (configurable via `ALERT_MIN_POSITION_PCT`) has a category above 80 or goes red overall, or if the portfolio composite exceeds 65. Smaller positions can be individually red without flagging the whole portfolio.

## Action suggestions

Separately from the risk score (which stays forward-looking), each position gets a deterministic partial take-profit / cut-loss suggestion based on unrealized P&L from entry. Fixed ladders pick a base tier — `ACTION_TP_LADDER` (default `25:25,50:50`: at +25% gain trim 25% of the position, at +50% trim 50%) and `ACTION_CL_LADDER` (default `-8:50,-15:100`: at −8% cut half, at −15% exit) — then the risk rating shifts the suggestion one tier: red escalates, green softens (a tier-1 signal on a green stock downgrades to hold), yellow keeps the base. Suggestions are candidates, not orders; Claude addresses each one in the report. Stateless: a standing suggestion repeats each run until acted on.

## Data sources

- **Yahoo Finance** — price history, fundamentals, news
- **Alpha Vantage** — AI-scored news sentiment from 50+ outlets (free API key)
- **Interactive Brokers** — live portfolio positions via Flex Query (optional)
