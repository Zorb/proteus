import os
import time
import schedule
import yfinance as yf
import pandas as pd
import anthropic
import asyncio
from telegram import Bot
from termcolor import colored, cprint
import config
from datetime import datetime

class StockRiskAgent:
    def __init__(self):
        """Initialize the Stock Risk Agent"""
        self.setup_telegram()
        self.setup_ai()
        self.portfolio_file = 'portfolio.csv'

        # Claude 3.5 Sonnet Pricing (per 1M tokens)
        self.input_price_per_mt = 3.00
        self.output_price_per_mt = 15.00

        cprint("🛡️ Stock Risk Agent Initialized", "white", "on_blue")

    def setup_telegram(self):
        """Initialize Telegram Bot"""
        try:
            self.bot_token = config.TELEGRAM_BOT_TOKEN
            self.chat_id = config.TELEGRAM_CHAT_ID
            if not self.bot_token or not self.chat_id:
                cprint("⚠️ Telegram credentials missing in .env", "yellow")
                self.bot = None
            else:
                self.bot = Bot(token=self.bot_token)
                cprint("📱 Telegram Bot Configured", "green")
        except Exception as e:
            cprint(f"❌ Telegram Setup Error: {e}", "red")
            self.bot = None

    def setup_ai(self):
        """Initialize Anthropic Client"""
        try:
            self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)
            cprint("🧠 Claude AI Configured", "green")
        except Exception as e:
            cprint(f"❌ AI Setup Error: {e}", "red")
            raise e

    def fetch_data(self, ticker):
        """Fetch market data from Yahoo Finance"""
        try:
            stock = yf.Ticker(ticker)

            # Get 1 month of history
            hist = stock.history(period="1mo")

            # Get news
            news = stock.news[:3] if stock.news else []
            news_summary = [n['title'] for n in news]

            # Get key info
            info = stock.info

            return {
                'ticker': ticker,
                'current_price': info.get('currentPrice', info.get('regularMarketPrice')),
                'history': hist.tail(5).to_dict(),  # Last 5 days
                'news': news_summary,
                'pe_ratio': info.get('trailingPE'),
                'market_cap': info.get('marketCap')
            }
        except Exception as e:
            cprint(f"❌ Error fetching data for {ticker}: {e}", "red")
            return None

    def calculate_cost(self, input_tokens, output_tokens):
        """Calculate estimated cost of the API call"""
        input_cost = (input_tokens / 1_000_000) * self.input_price_per_mt
        output_cost = (output_tokens / 1_000_000) * self.output_price_per_mt
        total_cost = input_cost + output_cost
        return total_cost

    def analyze_portfolio(self):
        """Main analysis logic"""
        cprint("\n📊 Starting Portfolio Analysis...", "cyan")

        # Read portfolio
        try:
            df = pd.read_csv(self.portfolio_file)
        except FileNotFoundError:
            cprint("❌ portfolio.csv not found!", "red")
            return

        portfolio_data = []

        for _, row in df.iterrows():
            ticker = row['Ticker']
            cprint(f"🔍 Fetching data for {ticker}...", "cyan")
            data = self.fetch_data(ticker)
            if data:
                portfolio_data.append(data)

        if not portfolio_data:
            cprint("❌ No data collected", "red")
            return

        # Prepare prompt
        prompt = config.RISK_PROMPT.format(data=str(portfolio_data))

        # Call Claude
        cprint("🤖 Sending data to Claude...", "magenta")
        try:
            message = self.client.messages.create(
                model=config.MODEL_NAME,
                max_tokens=4096,
                temperature=0.7,
                messages=[{"role": "user", "content": prompt}]
            )

            analysis = message.content[0].text

            # Calculate cost
            usage = message.usage
            cost = self.calculate_cost(usage.input_tokens, usage.output_tokens)

            cprint(f"💰 Analysis Cost: ${cost:.4f}", "yellow")

            # Send Report
            self.send_report(analysis, cost)

        except Exception as e:
            cprint(f"❌ AI Analysis Error: {e}", "red")

    def send_report(self, report, cost):
        """Send report via Telegram"""
        if not self.bot:
            cprint("⚠️ Telegram not configured, printing report:", "yellow")
            print(report)
            print(f"\n💰 Cost: ${cost:.4f}")
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer = f"\n\n🕒 {timestamp}\n💰 AI Cost: ${cost:.4f}"
        full_message = report + footer

        try:
            # Telegram has a message limit, split if needed (simple split)
            if len(full_message) > 4000:
                parts = [full_message[i:i+4000] for i in range(0, len(full_message), 4000)]
                for part in parts:
                    asyncio.run(self.bot.send_message(chat_id=self.chat_id, text=part, parse_mode='Markdown'))
            else:
                asyncio.run(self.bot.send_message(chat_id=self.chat_id, text=full_message, parse_mode='Markdown'))

            cprint("✅ Telegram Report Sent!", "green")
        except Exception as e:
            cprint(f"❌ Telegram Send Error: {e}", "red")

    def job(self):
        """Job to run on schedule"""
        cprint(f"⏰ Running scheduled analysis at {datetime.now()}", "cyan")
        self.analyze_portfolio()

    def run(self):
        """Start the scheduler"""
        cprint(f"🕰️ Scheduler started. Will run daily at {config.SCHEDULE_TIME}", "green")

        # Schedule the job
        schedule.every().day.at(config.SCHEDULE_TIME).do(self.job)

        # Also run once immediately on startup for verification
        self.job()

        while True:
            schedule.run_pending()
            time.sleep(60)

if __name__ == "__main__":
    agent = StockRiskAgent()
    agent.run()
