# 🛡️ Stock Risk Agent

An AI-powered agent that monitors your stock portfolio, assesses risk using Claude 3.5 Sonnet, and sends daily reports via Telegram.

## 🚀 Features
- **Daily Analysis**: Runs automatically at a scheduled time.
- **Yahoo Finance Data**: Fetches prices, news, and fundamentals.
- **Claude AI**: Provides deep risk assessment and recommendations.
- **Telegram Alerts**: Delivers reports directly to your phone.
- **Cost Tracking**: Shows the exact cost of each AI analysis.
- **Dockerized**: Easy to deploy on Raspberry Pi.

## 🛠️ Setup

1. **Configure Environment**:
   Create a `.env` file in this directory (or use the one passed via Docker):
   ```env
   TELEGRAM_BOT_TOKEN=your_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   ANTHROPIC_KEY=your_claude_key_here
   ```

2. **Set Portfolio**:
   Edit `portfolio.csv` with your stocks:
   ```csv
   Ticker,Position_Size,Avg_Price
   AAPL,10,150.00
   TSLA,5,200.00
   ```

3. **Run with Docker**:
   ```bash
   docker-compose up -d --build
   ```

## 📊 Output
The agent sends a Telegram message with:
- Risk Alerts
- Portfolio Overview
- Stock-by-Stock Analysis
- AI Cost Calculation

## 📝 Notes
- The schedule time is set in `config.py` (default 08:00).
- Ensure your Raspberry Pi has Internet access.
