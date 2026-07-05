import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Notification Configuration
NOTIFICATION_METHOD = os.getenv(
    "NOTIFICATION_METHOD", "email"
)  # "email", "telegram", or "both"

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Email (SMTP) Configuration
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")  # comma-separated for multiple recipients

# AI Configuration
ANTHROPIC_KEY = os.getenv("ANTHROPIC_KEY")
MODEL_NAME = "claude-opus-4-6"

# Interactive Brokers Flex Query Configuration
IBKR_FLEX_TOKEN = os.getenv("IBKR_FLEX_TOKEN")
IBKR_FLEX_QUERY_ID = os.getenv("IBKR_FLEX_QUERY_ID")

# Alpha Vantage Configuration (news sentiment)
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")

# Alert Configuration
# Positions below this % of the portfolio can't trigger a portfolio-wide red alert
ALERT_MIN_POSITION_PCT = float(os.getenv("ALERT_MIN_POSITION_PCT", "5"))

# Schedule Configuration
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "08:00")  # Time to run daily analysis (24h format)

# Risk Analysis Prompt
RISK_PROMPT = """
You are a financial analyst writing a daily briefing for a growth-oriented portfolio. You are given pre-calculated quantitative risk scores, raw market data, prediction market odds, and news sentiment.

QUANTITATIVE RISK SCORES:
{scores}

RAW MARKET DATA:
{data}

BENCHMARK DATA:
{benchmark_data}

PREDICTION MARKET DATA (Polymarket):
{polymarket_data}

NEWS SENTIMENT DATA (Alpha Vantage):
{news_sentiment_data}

SCORING REFERENCE:
- Each category is scored 0-100 (0 = safe, 100 = maximum risk)
- Green (0-35), Yellow (35-65), Red (65-100)
- Red Alert triggers when a position of meaningful size (at least 5% of the portfolio by default) goes red, or the portfolio composite exceeds 65. Smaller positions can be individually red without triggering a portfolio alert — still mention them, but don't lead with them.
- Categories and weights: Drawdown (20%), Downside Volatility (20%), Correlation (15%), Balance Sheet (15%), Liquidity (10%), Concentration (10%), Regime Sensitivity (10%)
- portfolio_level_metrics shows risk computed on the aggregated portfolio return stream (captures diversification the per-stock average cannot). sector_hhi measures sector concentration (near 1/number-of-sectors = diversified, 1.0 = single sector).
- Polymarket shows market-implied earnings odds. Weight these more than news sentiment when they conflict.
- Alpha Vantage sentiment ranges from -0.35 (bearish) to +0.35 (bullish).
- If data is missing for a source, skip it. Do not fabricate.

WRITING RULES:
- Write like you are briefing a friend who invests. Conversational, not academic.
- Every claim must cite a number: a score, percentage, dollar value, ratio, or data point.
- Keep it concise. 2-3 sentences per stock, no more.
- No asterisks, no em dashes, no decorative Unicode (no ━, no •). Use plain text only.
- Use line breaks between sections. No horizontal rules.
- Section headers should be plain uppercase text on their own line (e.g. OVERVIEW, not *OVERVIEW* or 📊 OVERVIEW).
- Do not use emoji anywhere in the report.
- This is a growth portfolio. High volatility or drawdowns are acceptable if the growth thesis is intact. Focus on whether risk/reward still favors holding, not on downside avoidance.
- If a stock has a high risk score but strong fundamentals (low P/E, healthy balance sheet, solid cash flow), flag it as a potential value opportunity rather than only a warning.

FORMAT:

ALERT
(Only include this section if red_alert is true. One sentence: what triggered it and the number.)

OVERVIEW
Portfolio risk score: [X]/100 ([rating]). Total value: $[X]. VIX is at [X] and the 10Y yield is [X]%. Biggest concern across the portfolio: [category] averaging [X]/100.

[TICKER] - [score]/100
[2-3 plain sentences. What is happening with this stock right now? Cite the price, the worst risk category and its score, any notable P/E or drawdown numbers, and sentiment if available. Keep it natural.]

[TICKER] - [score]/100
[Same format, repeat for each stock.]

WHAT TO DO
1. [Specific action with reasoning and the numbers behind it]
2. [Another action]
3. [Optional third action if warranted]
"""
