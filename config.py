import os
from dotenv import load_dotenv

import actions

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

# Action ladders: "pnl_threshold:trim_pct,..." — parsed at startup so a
# malformed .env fails loudly here, not mid-run. `or` (not a getenv default)
# so a present-but-blank var also falls back instead of crashing at import.
ACTION_TP_LADDER = actions.parse_ladder(
    os.getenv("ACTION_TP_LADDER") or "25:25,50:50", kind="gain"
)
ACTION_CL_LADDER = actions.parse_ladder(
    os.getenv("ACTION_CL_LADDER") or "-8:50,-15:100", kind="loss"
)
# Red-risk escalation caps past the top ladder tier (tunable beside the ladders)
ACTION_RED_TP_CAP = float(os.getenv("ACTION_RED_TP_CAP") or "75")
ACTION_RED_CL_CAP = float(os.getenv("ACTION_RED_CL_CAP") or "100")

# Schedule Configuration
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "08:00")  # Time to run daily analysis (24h format)

# Risk Analysis Prompt
RISK_PROMPT = """
You are a financial analyst writing a scheduled briefing for a growth-oriented portfolio (runs may be days apart, so frame changes since the previous run, not since yesterday). You are given pre-calculated quantitative risk scores, raw market data with entry prices and unrealized P&L, deterministic take-profit/cut-loss suggestions, and news sentiment.

QUANTITATIVE RISK SCORES:
{scores}

RAW MARKET DATA:
{data}

BENCHMARK DATA:
{benchmark_data}

NEWS SENTIMENT DATA (Alpha Vantage):
{news_sentiment_data}

SCORING REFERENCE:
- Each category is scored 0-100 (0 = safe, 100 = maximum risk)
- Green (0-35), Yellow (35-65), Red (65-100)
- Red Alert triggers when a position of meaningful size (at least 5% of the portfolio by default) goes red, or the portfolio composite exceeds 65. Smaller positions can be individually red without triggering a portfolio alert — still mention them, but don't lead with them.
- Categories and weights: Drawdown (20%), Downside Volatility (20%), Correlation (15%), Balance Sheet (15%), Liquidity (10%), Concentration (10%), Regime Sensitivity (10%)
- portfolio_level_metrics shows risk computed on the aggregated portfolio return stream (captures diversification the per-stock average cannot). sector_hhi measures sector concentration (near 1/number-of-sectors = diversified, 1.0 = single sector).
- Each stock carries action / trim_pct / action_reason: a deterministic partial take-profit or cut-loss suggestion from fixed unrealized-P&L tiers, shifted one tier by the risk rating (red harder, green softer). trim_pct is a percentage of the current position; position_size gives the share count, so express suggestions as both (e.g. "trim 25%, about 30 shares"). These are candidates, not orders — your job is judgment on top: endorse the trim size or push back, always citing a number.
- data_source "csv_fallback" means the live IBKR sync failed and positions come from the last saved snapshot — open the OVERVIEW by saying the position data may be stale.
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
[2-3 plain sentences. What is happening with this stock right now? Cite the price, the unrealized P&L from entry, the worst risk category and its score, any notable P/E or drawdown numbers, and sentiment if available. If the stock is a take_profit_candidate or cut_loss_candidate, address the suggested trim explicitly — endorse it or argue against it with a number. Keep it natural.]

[TICKER] - [score]/100
[Same format, repeat for each stock.]

WHAT TO DO
1. [Specific action with reasoning and the numbers behind it. Lead with any take-profit or cut-loss candidates, naming the suggested trim size — or your amended size if you disagree.]
2. [Another action]
3. [Optional third action if warranted]
"""
