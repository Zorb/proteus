import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# AI Configuration
ANTHROPIC_KEY = os.getenv('ANTHROPIC_KEY')
MODEL_NAME = "claude-3-5-sonnet-20240620"

# Schedule Configuration
SCHEDULE_TIME = "08:00"  # Time to run daily analysis (24h format)

# Risk Analysis Prompt
RISK_PROMPT = """
You are a professional financial risk analyst. Analyze the following stock portfolio data and provide a risk assessment.

Portfolio Data:
{data}

For each stock, consider:
1. Recent price action and volatility (based on OHLCV data).
2. Recent news sentiment (if available).
3. Key financial metrics (P/E, Market Cap, etc.).

Provide a summary report in the following format:

🚨 **Risk Alert** (if any immediate concerns)
...

📊 **Portfolio Overview**
...

🔍 **Stock-by-Stock Analysis**
- **[TICKER]**: [Risk Level: Low/Medium/High] - [Brief Analysis]

💡 **Recommendations**
...
"""
