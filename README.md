# Proteus

AI-powered stock portfolio risk monitor. Runs daily, scores your holdings across 7 risk categories, enriches with prediction market and news sentiment data, and delivers a plain-language briefing via email or Telegram.

## How it works

1. Loads your portfolio from Interactive Brokers (or a CSV fallback)
2. Fetches price history, fundamentals, and news from Yahoo Finance
3. Scores each stock 0-100 across 7 risk categories (drawdown, volatility, liquidity, balance sheet, correlation, concentration, regime sensitivity)
4. Pulls Polymarket earnings predictions and Alpha Vantage news sentiment
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

**Local:**
```bash
python stock_risk_agent.py
```

Runs once at startup, then daily at the scheduled time (default 08:00, configurable in `config.py`).

## Risk categories

| Category | Weight | What it measures |
|---|---|---|
| Drawdown | 20% | Max drawdown, distance from ATH, Ulcer Index |
| Downside Volatility | 20% | Sortino ratio, CVaR 5%, down-market beta |
| Correlation | 15% | SPY correlation, sector concentration |
| Balance Sheet | 15% | Debt/EBITDA, current ratio, interest coverage |
| Liquidity | 10% | Days to liquidate, volume trends |
| Concentration | 10% | Position size risk |
| Regime Sensitivity | 10% | Performance during rate spikes and VIX crises |

Each scored 0-100. Green (0-35), Yellow (35-65), Red (65-100). Alert triggers if any category exceeds 80 or the portfolio composite exceeds 65.

## Data sources

- **Yahoo Finance** — price history, fundamentals, news
- **Polymarket** — earnings prediction probabilities (free, no auth)
- **Alpha Vantage** — AI-scored news sentiment from 50+ outlets (free API key)
- **Interactive Brokers** — live portfolio positions via Flex Query (optional)
