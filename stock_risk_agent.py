import json
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import schedule
import yfinance as yf
import pandas as pd
import numpy as np
import anthropic
import asyncio
from telegram import Bot
from termcolor import colored, cprint
import config
import risk_framework as rf
import sentiment
import portfolio_sync
from datetime import datetime


def _retry(func, max_retries=3, base_delay=2, label=""):
    """Retry a function with exponential backoff. Returns result or raises last exception."""
    last_err = RuntimeError("No retries attempted")
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                cprint(
                    f"  ⚠️ {label} attempt {attempt}/{max_retries} failed: {e}. Retrying in {delay}s...",
                    "yellow",
                )
                time.sleep(delay)
            else:
                cprint(f"  ❌ {label} failed after {max_retries} attempts: {e}", "red")
    raise last_err


def _json_default(obj):
    """Serialize numpy/pandas scalars cleanly for the Claude prompt."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return str(obj)


class StockRiskAgent:
    def __init__(self):
        """Initialize the Stock Risk Agent"""
        self.setup_ai()
        self.portfolio_file = "portfolio.csv"

        cprint("🛡️ Stock Risk Agent Initialized", "white", "on_blue")

    def setup_ai(self):
        """Initialize Anthropic Client"""
        try:
            self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)
            cprint("🧠 Claude AI Configured", "green")
        except Exception as e:
            cprint(f"❌ AI Setup Error: {e}", "red")
            raise e

    def fetch_benchmarks(self):
        """Fetch benchmark data (SPY, VIX, TNX) — called once per analysis run."""
        cprint("📈 Fetching benchmark data (SPY, VIX, TNX)...", "cyan")
        benchmarks = {}
        try:
            # 3y windows so regime analysis can see past stress events,
            # not just the (possibly calm) trailing year
            spy_hist = _retry(
                lambda: yf.Ticker("SPY").history(period="3y"), label="SPY"
            )
            benchmarks["spy_hist"] = spy_hist
            benchmarks["spy_returns"] = rf.normalize_daily_index(
                spy_hist["Close"].pct_change().dropna()
            )
        except Exception as e:
            cprint(f"⚠️ SPY fetch error: {e}", "yellow")
            benchmarks["spy_hist"] = pd.DataFrame()
            benchmarks["spy_returns"] = pd.Series(dtype=float)

        try:
            vix_hist = _retry(
                lambda: yf.Ticker("^VIX").history(period="3y"), label="VIX"
            )
            benchmarks["vix_close"] = (
                rf.normalize_daily_index(vix_hist["Close"])
                if not vix_hist.empty
                else pd.Series(dtype=float)
            )
        except Exception as e:
            cprint(f"⚠️ VIX fetch error: {e}", "yellow")
            benchmarks["vix_close"] = pd.Series(dtype=float)

        try:
            tnx_hist = _retry(
                lambda: yf.Ticker("^TNX").history(period="3y"), label="TNX"
            )
            if not tnx_hist.empty:
                benchmarks["tnx_changes"] = rf.normalize_daily_index(
                    tnx_hist["Close"].pct_change().dropna()
                )
                benchmarks["risk_free_rate"] = (
                    tnx_hist["Close"].iloc[-1] / 100
                )  # TNX is in %
                benchmarks["tnx_available"] = True
            else:
                benchmarks["tnx_changes"] = pd.Series(dtype=float)
                benchmarks["risk_free_rate"] = 0.04
                benchmarks["tnx_available"] = False
        except Exception as e:
            cprint(f"⚠️ TNX fetch error: {e}", "yellow")
            benchmarks["tnx_changes"] = pd.Series(dtype=float)
            benchmarks["risk_free_rate"] = 0.04
            benchmarks["tnx_available"] = False

        cprint("✅ Benchmarks loaded", "green")
        return benchmarks

    def fetch_data(self, ticker):
        """Fetch expanded market data from Yahoo Finance."""
        try:
            stock = yf.Ticker(ticker)

            # Price history at multiple timeframes (with retry)
            hist_1y = _retry(lambda: stock.history(period="1y"), label=f"{ticker} 1y")
            hist_3y = _retry(lambda: stock.history(period="3y"), label=f"{ticker} 3y")
            hist_5y = _retry(lambda: stock.history(period="5y"), label=f"{ticker} 5y")

            # Recent closes for display (JSON-safe, no Timestamp keys)
            recent_closes = [
                {"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
                for d, c in hist_1y["Close"].tail(5).items()
            ]

            # News (yfinance .news can raise on some tickers)
            try:
                news = stock.news[:3] if stock.news else []
            except Exception:
                news = []
            news_summary = []
            for n in news:
                if isinstance(n, dict):
                    title = n.get("title", n.get("headline", "No title"))
                    news_summary.append(title)

            # Key info
            info = stock.info
            currency = info.get("currency", "USD")

            # Financials (may be empty for ETFs / some intl stocks)
            try:
                balance_sheet = stock.balance_sheet
            except Exception:
                balance_sheet = pd.DataFrame()

            try:
                income_stmt = stock.income_stmt
            except Exception:
                income_stmt = pd.DataFrame()

            # Volume averages for liquidity
            vol_30d = hist_1y["Volume"].tail(30).mean() if len(hist_1y) >= 30 else None
            vol_90d = hist_1y["Volume"].tail(90).mean() if len(hist_1y) >= 90 else None

            current_price = info.get("currentPrice", info.get("regularMarketPrice"))
            if currency == "GBp" and current_price:
                current_price = current_price / 100  # pence → pounds

            return {
                "ticker": ticker,
                "current_price": current_price,
                "recent_closes": recent_closes,
                "hist_1y": hist_1y,
                "hist_3y": hist_3y,
                "hist_5y": hist_5y,
                "news": news_summary,
                "pe_ratio": info.get("trailingPE"),
                "market_cap": info.get("marketCap"),
                "bid": info.get("bid"),
                "ask": info.get("ask"),
                "avg_volume": info.get("averageVolume"),
                "sector": info.get("sector"),
                "balance_sheet": balance_sheet,
                "income_stmt": income_stmt,
                "vol_30d": vol_30d,
                "vol_90d": vol_90d,
            }
        except Exception as e:
            cprint(f"❌ Error fetching data for {ticker}: {e}", "red")
            return None

    def log_token_usage(self, usage):
        """Log token usage from API call"""
        cprint(
            f"📊 Tokens — input: {usage.input_tokens}, output: {usage.output_tokens}",
            "yellow",
        )

    def analyze_portfolio(self):
        """Main analysis logic with quantitative risk scoring."""
        cprint("\n📊 Starting Portfolio Analysis...", "cyan")

        # Load portfolio (IBKR first, CSV fallback)
        df = None
        if config.IBKR_FLEX_TOKEN and config.IBKR_FLEX_QUERY_ID:
            cprint("🏦 Fetching portfolio from Interactive Brokers...", "cyan")
            df = portfolio_sync.fetch_ibkr_portfolio()
            if df is not None:
                cprint(f"✅ Portfolio loaded from IBKR ({len(df)} positions)", "green")
            else:
                cprint("⚠️ IBKR fetch failed, falling back to CSV", "yellow")

        if df is None:
            try:
                df = pd.read_csv(self.portfolio_file)
                cprint(f"📄 Portfolio loaded from CSV ({len(df)} positions)", "cyan")
            except FileNotFoundError:
                cprint("❌ portfolio.csv not found and IBKR not configured!", "red")
                return

        # Fetch benchmarks once
        benchmarks = self.fetch_benchmarks()

        # Fetch all stock data
        portfolio_data = []
        skipped_tickers = []
        for _, row in df.iterrows():
            ticker = row["Ticker"]
            cprint(f"🔍 Fetching data for {ticker}...", "cyan")
            data = self.fetch_data(ticker)
            if data:
                data["position_size"] = row["Position_Size"]
                data["avg_price"] = row["Avg_Price"]
                portfolio_data.append(data)
            else:
                skipped_tickers.append(ticker)

        if skipped_tickers:
            cprint(
                f"⚠️ Excluded from analysis (data fetch failed): {', '.join(skipped_tickers)}",
                "yellow",
            )

        if not portfolio_data:
            cprint("❌ No data collected", "red")
            return

        # Compute portfolio-level values
        total_value = 0
        for d in portfolio_data:
            price = d["current_price"] or d["avg_price"]
            d["position_value"] = d["position_size"] * price
            total_value += d["position_value"]

        for d in portfolio_data:
            d["position_pct"] = (
                (d["position_value"] / total_value * 100) if total_value > 0 else 0
            )

        # Build sector weights for value-weighted HHI
        sector_value_map = {}
        for d in portfolio_data:
            sector = d.get("sector")
            if sector:
                sector_value_map[sector] = (
                    sector_value_map.get(sector, 0) + d["position_value"]
                )
        sector_weights = (
            {s: v / total_value for s, v in sector_value_map.items()}
            if total_value > 0
            else {}
        )

        # Build portfolio returns (value-weighted, aligned all at once)
        returns_dict = {}
        weights_dict = {}
        for d in portfolio_data:
            if d["hist_1y"] is not None and len(d["hist_1y"]) > 0:
                returns_dict[d["ticker"]] = rf.normalize_daily_index(
                    d["hist_1y"]["Close"].pct_change().dropna()
                )
                weights_dict[d["ticker"]] = d["position_pct"] / 100

        # Renormalize weights over tickers that actually have history, so a
        # ticker without data doesn't silently shrink portfolio moves
        if returns_dict:
            all_returns_df = pd.DataFrame(returns_dict).dropna()
            w_total = sum(weights_dict[t] for t in all_returns_df.columns)
            portfolio_returns = (
                sum(
                    all_returns_df[t] * weights_dict[t] / w_total
                    for t in all_returns_df.columns
                )
                if w_total > 0
                else None
            )
        else:
            all_returns_df = None
            portfolio_returns = None

        # Score each stock
        cprint("🧮 Computing risk scores...", "magenta")
        all_scores = []
        for d in portfolio_data:
            ticker = d["ticker"]
            returns_1y = returns_dict.get(ticker)
            spy_ret = benchmarks["spy_returns"]

            # Individual metrics
            drawdown = rf.score_drawdown_profile(
                d["hist_1y"], d["hist_3y"], d["hist_5y"]
            )
            downside = rf.score_downside_volatility(
                returns_1y, spy_ret, benchmarks["risk_free_rate"]
            )
            liquidity = rf.score_liquidity_risk(
                d["position_size"],
                d["current_price"],
                d["avg_volume"],
                d["bid"],
                d["ask"],
                d["vol_30d"],
                d["vol_90d"],
            )
            balance = rf.score_balance_sheet(d["balance_sheet"], d["income_stmt"])

            # Individual average for concentration risk input
            individual_avg = np.mean(
                [
                    drawdown["score"],
                    downside["score"],
                    liquidity["score"],
                    balance["score"],
                ]
            )

            # Portfolio excluding this stock (leave-one-out) — a large position
            # otherwise correlates mechanically with the portfolio it dominates
            portfolio_ex_stock = None
            if all_returns_df is not None and ticker in all_returns_df.columns:
                others = [t for t in all_returns_df.columns if t != ticker]
                w_others = sum(weights_dict[t] for t in others)
                if others and w_others > 0:
                    portfolio_ex_stock = sum(
                        all_returns_df[t] * weights_dict[t] / w_others for t in others
                    )

            correlation = rf.score_correlation_risk(
                returns_1y, portfolio_ex_stock, spy_ret, d.get("sector"), sector_weights
            )
            concentration = rf.score_concentration_risk(
                d["position_pct"], individual_avg
            )
            # 3y returns so the regime window contains actual stress events
            returns_3y = (
                rf.normalize_daily_index(d["hist_3y"]["Close"].pct_change().dropna())
                if d["hist_3y"] is not None and len(d["hist_3y"]) > 0
                else returns_1y
            )
            regime = rf.score_regime_sensitivity(
                returns_3y, benchmarks["tnx_changes"], benchmarks["vix_close"]
            )

            scores = {
                "drawdown_profile": drawdown,
                "downside_volatility": downside,
                "liquidity_risk": liquidity,
                "balance_sheet": balance,
                "correlation_risk": correlation,
                "concentration_risk": concentration,
                "regime_sensitivity": regime,
            }
            composite = rf.calculate_composite(scores)

            stock_scores = {
                "ticker": ticker,
                "position_pct": round(d["position_pct"], 1),
                "current_price": d["current_price"],
                "scores": scores,
                "composite": composite,
            }
            all_scores.append(stock_scores)
            cprint(
                f"  {ticker}: composite {composite['composite_score']} ({composite['rating']})",
                "cyan",
            )

        # Portfolio composite (weighted average of stock composites by position %)
        portfolio_composite = sum(
            s["composite"]["composite_score"] * s["position_pct"] / 100
            for s in all_scores
        )
        portfolio_composite = round(portfolio_composite)
        portfolio_red_alert = rf.portfolio_red_alert(
            all_scores, portfolio_composite, config.ALERT_MIN_POSITION_PCT
        )

        # True portfolio-level risk from aggregated returns — the weighted
        # average of per-stock composites can't see diversification benefit
        portfolio_level = rf.score_portfolio_level(
            portfolio_returns, benchmarks["spy_returns"], benchmarks["risk_free_rate"]
        )
        sector_hhi = (
            round(sum(w**2 for w in sector_weights.values()), 3)
            if sector_weights
            else None
        )

        portfolio_summary = {
            "total_value": round(total_value, 2),
            "portfolio_composite_score": portfolio_composite,
            "portfolio_rating": rf._rating(portfolio_composite),
            "portfolio_red_alert": portfolio_red_alert,
            "sector_hhi": sector_hhi,
            "portfolio_level_metrics": portfolio_level,
            "stocks": all_scores,
        }
        if skipped_tickers:
            portfolio_summary["excluded_tickers_data_unavailable"] = skipped_tickers

        # Build display data for Claude (raw data subset — no full DataFrames)
        display_data = []
        for d in portfolio_data:
            display_data.append(
                {
                    "ticker": d["ticker"],
                    "current_price": d["current_price"],
                    "position_size": d["position_size"],
                    "position_pct": round(d["position_pct"], 1),
                    "news": d["news"],
                    "pe_ratio": d["pe_ratio"],
                    "market_cap": d["market_cap"],
                    "sector": d.get("sector"),
                    "recent_closes": d["recent_closes"],
                }
            )

        # Fetch Polymarket earnings data
        cprint("🎰 Fetching Polymarket earnings data...", "cyan")
        tickers = [d["ticker"] for d in portfolio_data]
        polymarket_results = sentiment.fetch_polymarket_earnings(tickers)
        polymarket_str = sentiment.format_polymarket_data(polymarket_results)
        cprint("✅ Polymarket data loaded", "green")

        # Fetch Alpha Vantage news sentiment
        cprint("📰 Fetching news sentiment...", "cyan")
        try:
            news_results = sentiment.fetch_news_sentiment(
                tickers, config.ALPHAVANTAGE_API_KEY
            )
            news_str = sentiment.format_news_sentiment(news_results)
            cprint("✅ News sentiment loaded", "green")
        except Exception as e:
            cprint(f"  ⚠️ News sentiment fetch failed: {e}", "yellow")
            news_str = "News sentiment data unavailable."

        # Build benchmark summary for Claude
        vix_current = (
            round(benchmarks["vix_close"].iloc[-1], 2)
            if not benchmarks["vix_close"].empty
            else "unavailable"
        )
        tnx_current = (
            round(benchmarks["risk_free_rate"] * 100, 2)
            if benchmarks["tnx_available"]
            else "unavailable"
        )
        benchmark_str = f"VIX: {vix_current}, 10Y Treasury Yield: {tnx_current}%"

        # Prepare prompt (JSON, not str() — numpy scalars repr as np.float64(...) otherwise)
        prompt = config.RISK_PROMPT.format(
            scores=json.dumps(portfolio_summary, default=_json_default, indent=1),
            data=json.dumps(display_data, default=_json_default, indent=1),
            polymarket_data=polymarket_str,
            news_sentiment_data=news_str,
            benchmark_data=benchmark_str,
        )

        # Call Claude with extended thinking
        cprint(
            "🤖 Sending scores + data to Claude (Opus 4 + extended thinking)...",
            "magenta",
        )
        try:
            message = _retry(
                lambda: self.client.messages.create(
                    model=config.MODEL_NAME,
                    max_tokens=16000,
                    temperature=1,  # required for extended thinking
                    thinking={
                        "type": "enabled",
                        "budget_tokens": 10000,
                    },
                    messages=[{"role": "user", "content": prompt}],
                ),
                max_retries=2,
                base_delay=5,
                label="Claude API",
            )

            # Extract text block (skip thinking blocks)
            analysis = next(
                (block.text for block in message.content if block.type == "text"),
                None,
            )
            if analysis is None:
                cprint("❌ Claude response contained no text block", "red")
                return

            # Log token usage
            self.log_token_usage(message.usage)

            # Send Report
            self.send_report(analysis, portfolio_summary)

        except Exception as e:
            cprint(f"❌ AI Analysis Error: {e}", "red")

    def send_report(self, report, portfolio_summary=None):
        """Route report to configured notification method(s)"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer = f"\n\n🕒 {timestamp}"
        full_message = report + footer

        method = (config.NOTIFICATION_METHOD or "email").lower()

        if method == "email":
            self.send_email(full_message, portfolio_summary)
        elif method == "telegram":
            self.send_telegram(full_message)
        elif method == "both":
            self.send_email(full_message, portfolio_summary)
            self.send_telegram(full_message)
        else:
            cprint(
                f"⚠️ Unknown NOTIFICATION_METHOD '{method}', printing report:", "yellow"
            )
            print(full_message)

    def format_html_report(self, plain_text, portfolio_summary=None):
        """Convert plain-text report to styled HTML email."""
        import html as html_mod
        import re

        escaped = html_mod.escape(plain_text)

        # Convert lines to HTML with section headers detected
        lines = escaped.split("\n")
        html_lines = []
        # Match ALL-CAPS section headers (e.g. OVERVIEW, WHAT TO DO, ALERT)
        # and ticker lines like "AMD - 42/100"
        header_re = re.compile(r"^[A-Z][A-Z .]{2,}$")
        ticker_re = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?\s*-\s*\d+/100")

        for line in lines:
            stripped = line.strip()
            if not stripped:
                html_lines.append("<br>")
                continue
            if header_re.match(stripped):
                html_lines.append(
                    f'<h3 style="margin: 20px 0 8px; font-size: 14px; color: #555; letter-spacing: 1px; border-bottom: 1px solid #eee; padding-bottom: 4px;">{stripped}</h3>'
                )
                continue
            if ticker_re.match(stripped):
                html_lines.append(
                    f'<p style="margin: 14px 0 4px; font-weight: bold; font-size: 15px; color: #1a1a2e;">{stripped}</p>'
                )
                continue
            # Numbered action items (1. 2. 3.)
            if re.match(r"^\d+\.\s", stripped):
                html_lines.append(
                    f'<p style="margin: 4px 0 4px 12px; line-height: 1.5;">{stripped}</p>'
                )
                continue
            html_lines.append(
                f'<p style="margin: 4px 0; line-height: 1.5;">{stripped}</p>'
            )

        body_content = "\n".join(html_lines)

        # Build risk summary table if portfolio_summary is available
        risk_table = ""
        if portfolio_summary and "stocks" in portfolio_summary:
            composite = portfolio_summary.get("portfolio_composite_score", 0)
            rating = portfolio_summary.get("portfolio_rating", "green")
            badge_color = {
                "green": "#27ae60",
                "yellow": "#f39c12",
                "red": "#e74c3c",
            }.get(rating, "#999")

            rows = ""
            for stock in portfolio_summary["stocks"]:
                s_rating = stock["composite"]["rating"]
                s_color = {
                    "green": "#27ae60",
                    "yellow": "#f39c12",
                    "red": "#e74c3c",
                }.get(s_rating, "#999")
                s_score = stock["composite"]["composite_score"]
                rows += f"""
                <tr>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; font-weight: bold;">{stock["ticker"]}</td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: center;">
                        <span style="background: {s_color}; color: white; padding: 2px 10px; border-radius: 12px; font-size: 13px;">{s_score}/100</span>
                    </td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: right;">{stock["position_pct"]}%</td>
                </tr>"""

            risk_table = f"""
            <div style="margin: 20px 0;">
                <div style="text-align: center; margin-bottom: 16px;">
                    <span style="font-size: 14px; color: #666;">Portfolio Composite</span><br>
                    <span style="background: {badge_color}; color: white; padding: 6px 20px; border-radius: 16px; font-size: 20px; font-weight: bold; display: inline-block; margin-top: 8px;">{composite}/100</span>
                </div>
                <table style="width: 100%; border-collapse: collapse; font-family: monospace;">
                    <tr style="background: #f8f9fa;">
                        <th style="padding: 8px 12px; text-align: left; border-bottom: 2px solid #ddd;">Ticker</th>
                        <th style="padding: 8px 12px; text-align: center; border-bottom: 2px solid #ddd;">Risk Score</th>
                        <th style="padding: 8px 12px; text-align: right; border-bottom: 2px solid #ddd;">Weight</th>
                    </tr>
                    {rows}
                </table>
            </div>"""

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 680px; margin: 0 auto; padding: 20px; color: #333; background: #fff;">
    <div style="background: #1a1a2e; color: white; padding: 20px; border-radius: 8px 8px 0 0; text-align: center;">
        <h1 style="margin: 0; font-size: 22px;">Proteus Risk Report</h1>
        <p style="margin: 4px 0 0; color: #aaa; font-size: 13px;">{datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
    </div>
    <div style="border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 8px 8px; padding: 20px;">
        {risk_table}
        <hr style="border: 1px solid #eee; margin: 20px 0;">
        {body_content}
    </div>
    <p style="text-align: center; color: #999; font-size: 11px; margin-top: 16px;">Generated by Proteus</p>
</body>
</html>"""
        return html

    def send_email(self, message, portfolio_summary=None):
        """Send report via SMTP email with HTML formatting and plain-text fallback"""
        smtp_user = config.SMTP_USER
        smtp_password = config.SMTP_PASSWORD
        email_to = config.EMAIL_TO

        if not smtp_user or not smtp_password or not email_to:
            cprint("⚠️ Email not configured, printing report:", "yellow")
            print(message)
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = config.EMAIL_FROM or smtp_user
            recipients = [addr.strip() for addr in email_to.split(",")]
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = (
                f"Proteus Risk Report - {datetime.now().strftime('%Y-%m-%d')}"
            )

            # Plain-text fallback
            msg.attach(MIMEText(message, "plain", "utf-8"))

            # HTML version (preferred by email clients)
            html_body = self.format_html_report(message, portfolio_summary)
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())

            cprint("✅ Email Report Sent!", "green")
        except Exception as e:
            cprint(f"❌ Email Send Error: {e}", "red")

    def send_telegram(self, message):
        """Send report via Telegram"""
        bot_token = config.TELEGRAM_BOT_TOKEN
        chat_id = config.TELEGRAM_CHAT_ID

        if not bot_token or not chat_id:
            cprint("⚠️ Telegram not configured, printing report:", "yellow")
            print(message)
            return

        async def _send(content):
            bot = Bot(token=bot_token)
            try:
                max_length = 4000
                parts = []
                while content:
                    if len(content) <= max_length:
                        parts.append(content)
                        break

                    split_idx = content.rfind("\n\n", 0, max_length)
                    if split_idx == -1:
                        split_idx = content.rfind("\n", 0, max_length)
                    if split_idx == -1:
                        split_idx = max_length

                    parts.append(content[:split_idx])
                    content = content[split_idx:].lstrip()

                for part in parts:
                    await bot.send_message(chat_id=chat_id, text=part)

                cprint("✅ Telegram Report Sent!", "green")
            except Exception as e:
                cprint(f"❌ Telegram Send Error: {e}", "red")

        try:
            asyncio.run(_send(message))
        except Exception as e:
            cprint(f"❌ Telegram Async Error: {e}", "red")

    def job(self):
        """Job to run on schedule"""
        cprint(f"⏰ Running scheduled analysis at {datetime.now()}", "cyan")
        try:
            self.analyze_portfolio()
        except Exception as e:
            # An uncaught exception here would kill the scheduler loop for good
            cprint(f"❌ Analysis run failed: {e}", "red")

    def run(self):
        """Start the scheduler"""
        cprint(
            f"🕰️ Scheduler started. Will run daily at {config.SCHEDULE_TIME}", "green"
        )

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
