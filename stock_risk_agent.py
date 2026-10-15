import json
import math
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
import actions
import sentiment
import portfolio_sync

# Single source for report colors — used by the score badges and the
# Entry/P&L/Action cells alike
RATING_COLORS = {"green": "#27ae60", "yellow": "#f39c12", "red": "#e74c3c"}
MUTED_COLOR = "#999"
from datetime import datetime


class AnalysisError(RuntimeError):
    def __init__(self, message, report_sent=False):
        super().__init__(message)
        self.report_sent = report_sent


class ReportDeliveryError(RuntimeError):
    pass


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _validated_history(ticker, period):
    """Validate inside retries: empty, corrupt and stale responses are failures."""
    def fetch():
        hist = ticker.history(period=period)
        if hist is None or hist.empty or "Close" not in hist:
            raise ValueError(f"{period} price history unavailable")
        hist = rf.normalize_daily_index(hist).sort_index()
        if hist.index.has_duplicates or not np.isfinite(hist["Close"]).all() or not (hist["Close"] > 0).all():
            raise ValueError(f"{period} price history contains invalid observations")
        age = (pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - hist.index[-1]).days
        if age < 0 or age > 7:
            raise ValueError(f"{period} price history is stale or future-dated")
        return hist
    return _retry(fetch, label=f"{getattr(ticker, 'ticker', 'benchmark')} {period}")


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
        # Market analysis and deterministic warnings must work even without AI.
        self.client = None
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
            spy_hist = _validated_history(yf.Ticker("SPY"), "3y")
            benchmarks["spy_hist"] = spy_hist
            benchmarks["spy_returns"] = rf.normalize_daily_index(
                spy_hist["Close"].pct_change(fill_method=None).dropna()
            )
        except Exception as e:
            cprint(f"⚠️ SPY fetch error: {e}", "yellow")
            benchmarks["spy_hist"] = pd.DataFrame()
            benchmarks["spy_returns"] = pd.Series(dtype=float)

        try:
            vix_hist = _validated_history(yf.Ticker("^VIX"), "3y")
            benchmarks["vix_close"] = (
                rf.normalize_daily_index(vix_hist["Close"])
                if not vix_hist.empty
                else pd.Series(dtype=float)
            )
        except Exception as e:
            cprint(f"⚠️ VIX fetch error: {e}", "yellow")
            benchmarks["vix_close"] = pd.Series(dtype=float)

        try:
            tnx_hist = _validated_history(yf.Ticker("^TNX"), "3y")
            if not tnx_hist.empty:
                benchmarks["tnx_changes"] = rf.normalize_daily_index(
                    tnx_hist["Close"].pct_change(fill_method=None).dropna()
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
            histories = {}
            quality_issues = []
            for period, minimum in (("1y", 200), ("3y", 600), ("5y", 1000)):
                try:
                    histories[period] = _validated_history(stock, period)
                    if len(histories[period]) < minimum:
                        quality_issues.append(f"{period} history only has {len(histories[period])} observations")
                except Exception as e:
                    histories[period] = pd.DataFrame(columns=["Close", "Volume"])
                    quality_issues.append(f"{period} history unavailable: {e}")
            hist_1y, hist_3y, hist_5y = (histories[p] for p in ("1y", "3y", "5y"))

            # Recent closes for display (JSON-safe, no Timestamp keys)
            recent_closes = [
                {"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
                for d, c in hist_1y["Close"].tail(5).items()
            ]

            # Key info
            info = stock.info
            currency = info.get("currency")

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
            volume = hist_1y.get("Volume", pd.Series(dtype=float))
            vol_30d = volume.tail(30).mean() if len(volume) >= 30 else None
            vol_90d = volume.tail(90).mean() if len(volume) >= 90 else None

            current_price = _positive_number(info.get("currentPrice")) or _positive_number(info.get("regularMarketPrice"))
            price_source = "quote"
            quote_time = _positive_number(info.get("regularMarketTime"))
            if quote_time is not None and (time.time() - quote_time > 7 * 86400 or quote_time > time.time() + 86400):
                current_price = None
            if current_price is None and not hist_1y.empty:
                current_price = _positive_number(hist_1y["Close"].iloc[-1])
                price_source = "latest_validated_close"
            if quote_time is None and price_source == "quote":
                quality_issues.append("quote timestamp unavailable")

            return {
                "ticker": ticker,
                "currency": currency,
                "current_price": current_price,
                "price_source": price_source,
                "data_quality_issues": quality_issues,
                "recent_closes": recent_closes,
                "hist_1y": hist_1y,
                "hist_3y": hist_3y,
                "hist_5y": hist_5y,
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

        # Load portfolio (IBKR first with retries, CSV fallback)
        df = None
        data_source = None
        if config.IBKR_FLEX_TOKEN and config.IBKR_FLEX_QUERY_ID:
            cprint("🏦 Fetching portfolio from Interactive Brokers...", "cyan")
            for attempt in range(3):
                if attempt:
                    cprint(
                        f"  🔁 IBKR retry {attempt + 1}/3 in 90s (transient maintenance windows pass)...",
                        "yellow",
                    )
                    time.sleep(90)
                df = portfolio_sync.fetch_ibkr_portfolio()
                if df is not None:
                    break
            if df is not None:
                df = portfolio_sync.normalize_positions(df)
                data_source = "ibkr"
                cprint(f"✅ Portfolio loaded from IBKR ({len(df)} positions)", "green")
                # Mirror the live portfolio so the CSV fallback is never staler
                # than the last successful sync
                try:
                    df.to_csv(self.portfolio_file, index=False)
                except OSError as e:
                    cprint(f"  ⚠️ Could not refresh {self.portfolio_file}: {e}", "yellow")
            else:
                cprint("⚠️ IBKR fetch failed after 3 attempts, falling back to CSV", "yellow")

        if df is None:
            try:
                df = pd.read_csv(self.portfolio_file)
                data_source = "csv_fallback" if config.IBKR_FLEX_TOKEN and config.IBKR_FLEX_QUERY_ID else "csv"
                cprint(
                    f"📄 Portfolio loaded from CSV snapshot ({len(df)} positions) — may be stale",
                    "yellow",
                )
            except FileNotFoundError:
                raise AnalysisError("No usable broker portfolio or portfolio.csv is available")

        df = portfolio_sync.normalize_positions(df)
        if df.empty:
            summary = {"data_source": data_source, "currency": "USD", "total_value": 0,
                       "portfolio_composite_score": None, "portfolio_rating": "unavailable",
                       "portfolio_red_alert": False, "stocks": []}
            self.send_report("OVERVIEW\nNo stock positions in the loaded portfolio. No keep or trim decisions are needed. Cash is outside this stock analysis.", summary)
            return summary

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
                avg_price = row["Avg_Price"]
                data["avg_price"] = _positive_number(avg_price)
                if data.get("currency") != "USD":
                    raise AnalysisError(f"{ticker}: market data currency must be USD; received {data.get('currency')!r}")
                data["current_price"] = _positive_number(data.get("current_price"))
                if data["current_price"] is None:
                    raise AnalysisError(f"{ticker}: current valuation unavailable; entry cost cannot substitute for market value")
                portfolio_data.append(data)
            else:
                skipped_tickers.append(ticker)

        if skipped_tickers:
            raise AnalysisError(f"Market data unavailable for {', '.join(skipped_tickers)}; portfolio weights and keep/trim suggestions withheld")

        if not portfolio_data:
            raise AnalysisError("No market data collected")

        # Compute portfolio-level values
        total_value = 0
        for d in portfolio_data:
            price = d["current_price"]
            d["position_value"] = d["position_size"] * price
            total_value += d["position_value"]

        if not math.isfinite(total_value) or total_value <= 0:
            raise AnalysisError("Portfolio market value must be finite and positive")

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

        # Individual histories retain their own observations. Portfolio metrics
        # require full coverage instead of deleting gaps or reweighting holdings.
        returns_dict = {}
        weights_dict = {d["ticker"]: d["position_pct"] / 100 for d in portfolio_data}
        for d in portfolio_data:
            if d["hist_1y"] is not None and len(d["hist_1y"]) > 0:
                returns_dict[d["ticker"]] = rf.normalize_daily_index(
                    d["hist_1y"]["Close"].pct_change(fill_method=None).dropna()
                )

        all_returns_df, portfolio_returns, return_coverage = rf.build_portfolio_returns(
            {d["ticker"]: d["hist_1y"] for d in portfolio_data}, weights_dict, benchmarks["spy_returns"]
        )

        # Score each stock
        cprint("🧮 Computing risk scores...", "magenta")
        all_scores = []
        display_data = []
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
                rf.normalize_daily_index(d["hist_3y"]["Close"].pct_change(fill_method=None).dropna())
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
            quality_issues = list(d.get("data_quality_issues", []))
            quality_issues.extend(f"{category}: incomplete data" for category in composite["incomplete_categories"])
            if not return_coverage["data_available"]:
                quality_issues.append("portfolio history has gaps; portfolio correlation unavailable")
            if not benchmarks["tnx_available"]:
                quality_issues.append("Treasury yield unavailable; 4% risk-free assumption used")
            if data_source == "csv_fallback":
                quality_issues.append("broker sync unavailable; verify snapshot holdings before acting")
            composite["data_complete"] = not quality_issues

            # Entry-aware action layer — risk score stays independent of entry,
            # this only suggests partial exits from unrealized P&L + rating
            pnl_pct = actions.unrealized_pnl_pct(d["current_price"], d["avg_price"])
            action = actions.evaluate_position_action(
                pnl_pct,
                composite["rating"],
                config.ACTION_TP_LADDER,
                config.ACTION_CL_LADDER,
                red_tp_cap=config.ACTION_RED_TP_CAP,
                red_cl_cap=config.ACTION_RED_CL_CAP,
                critical_alert=composite["critical_alert"],
                data_complete=composite["data_complete"],
            )

            stock_scores = {
                "ticker": ticker,
                "position_pct": d["position_pct"],
                "position_size": d["position_size"],
                "current_price": d["current_price"],
                "avg_price": d["avg_price"],
                "unrealized_pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else None,
                "action": action["action"],
                "trim_pct": action["trim_pct"],
                "action_reason": action["reason"],
                "data_quality_issues": quality_issues,
                "scores": scores,
                "composite": composite,
            }
            all_scores.append(stock_scores)
            # Raw-data subset for the prompt's {data} slot; action fields live
            # only on the scores entry so the prompt carries them once
            display_data.append(
                {
                    "ticker": ticker,
                    "current_price": d["current_price"],
                    "currency": "USD",
                    "price_source": d.get("price_source", "quote"),
                    "position_pct": round(d["position_pct"], 1),
                    "pe_ratio": d["pe_ratio"],
                    "market_cap": d["market_cap"],
                    "sector": d.get("sector"),
                    "recent_closes": d["recent_closes"],
                }
            )
            cprint(
                f"  {ticker}: composite {composite['composite_score']} ({composite['rating']})",
                "cyan",
            )
            if action["action"] in ("take_profit_candidate", "cut_loss_candidate"):
                cprint(
                    f"    ↳ {action['action']}: trim {action['trim_pct']:g}% — {action['reason']}",
                    "yellow",
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
            "data_source": data_source,
            "currency": "USD",
            "total_value": round(total_value, 2),
            "portfolio_composite_score": portfolio_composite,
            "portfolio_rating": rf._rating(portfolio_composite),
            "portfolio_red_alert": portfolio_red_alert,
            "sector_hhi": sector_hhi,
            "portfolio_level_metrics": portfolio_level,
            "portfolio_return_coverage": return_coverage,
            "data_complete": all(s["composite"]["data_complete"] for s in all_scores),
            "stocks": all_scores,
        }
        if skipped_tickers:
            portfolio_summary["excluded_tickers_data_unavailable"] = skipped_tickers

        tickers = [d["ticker"] for d in portfolio_data]

        # Fetch recent news headlines (Yahoo Finance RSS)
        cprint("📰 Fetching news headlines...", "cyan")
        try:
            news_results = sentiment.fetch_news(tickers)
            news_str = sentiment.format_news(news_results)
            with_news = sum(1 for headlines in news_results.values() if headlines)
            if with_news:
                cprint(f"✅ News headlines loaded for {with_news}/{len(tickers)} tickers", "green")
            else:
                cprint("  ⚠️ No news headlines for any ticker", "yellow")
        except Exception as e:
            cprint(f"  ⚠️ News headlines fetch failed: {e}", "yellow")
            news_str = "News headlines unavailable."

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
            news_data=news_str,
            benchmark_data=benchmark_str,
        )

        # Call Claude with extended thinking
        cprint(
            "🤖 Sending scores + data to Claude (Opus 4 + extended thinking)...",
            "magenta",
        )
        try:
            if self.client is None:
                self.setup_ai()
            message = _retry(
                lambda: self.client.messages.create(
                    model=config.MODEL_NAME,
                    max_tokens=16000,
                    temperature=1,  # required for extended thinking
                    thinking={
                        "type": "adaptive",
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
            if not analysis or not analysis.strip() or getattr(message, "stop_reason", None) == "max_tokens":
                raise AnalysisError("Claude returned an empty or truncated analysis")

            # Log token usage
            self.log_token_usage(message.usage)

        except Exception as e:
            cprint(f"❌ AI Analysis Error: {e}", "red")
            self.send_report(self.deterministic_report(portfolio_summary), portfolio_summary)
            raise AnalysisError("AI interpretation failed; quantitative fallback report delivered", report_sent=True) from e

        self.send_report(analysis, portfolio_summary)
        return portfolio_summary

    def deterministic_report(self, summary):
        """A usable numerical briefing when the AI service is unavailable."""
        lines = ["AI INTERPRETATION UNAVAILABLE", "Quantitative candidates only; review the evidence before acting.",
                 "OVERVIEW", f"USD stock value: ${summary['total_value']:,.2f}. Portfolio score: {summary['portfolio_composite_score']}/100."]
        for stock in summary["stocks"]:
            lines.extend([f"{stock['ticker']} - {stock['composite']['composite_score']}/100",
                          f"Weight {stock['position_pct']:.1f}%. {actions.action_label(stock['action'], stock['trim_pct'], stock['position_size'])}. {stock['action_reason']}"])
        return "\n".join(lines)

    def report_notices(self, summary):
        """Deterministic alerts shared by HTML, plain email and Telegram."""
        if not summary:
            return ""
        lines = []
        if summary.get("portfolio_red_alert"):
            affected = [s["ticker"] for s in summary["stocks"] if s["composite"]["red_alert"] and s["position_pct"] >= config.ALERT_MIN_POSITION_PCT]
            lines.append(f"RISK ALERT\nPortfolio score {summary['portfolio_composite_score']}/100; significant stock alerts: {', '.join(affected) or 'none'}. Review flagged positions before deciding to keep or trim.")
        if summary.get("data_source") == "csv_fallback":
            lines.append("POSITION DATA WARNING\nBroker sync failed. Verify holdings against the saved snapshot before acting.")
        for stock in summary.get("stocks", []):
            if stock.get("data_quality_issues"):
                lines.append(f"DATA QUALITY: {stock['ticker']}: " + "; ".join(stock["data_quality_issues"]))
            if stock["composite"].get("critical_alert"):
                critical = [name for name, value in stock["scores"].items() if value["score"] > 80]
                values = [f"{name} {stock['scores'][name]['score']}/100" for name in critical]
                lines.append(f"CRITICAL CATEGORY: {stock['ticker']}: {', '.join(values)}. Composite colour does not override this alert.")
        return "\n\n".join(lines)

    def send_report(self, report, portfolio_summary=None):
        """Route report to configured notification method(s)"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer = f"\n\n🕒 {timestamp}"
        notices = self.report_notices(portfolio_summary)
        full_message = (notices + "\n\n" if notices else "") + report + footer

        method = (config.NOTIFICATION_METHOD or "email").lower()

        if method not in ("email", "telegram", "both"):
            raise ReportDeliveryError(f"Unknown NOTIFICATION_METHOD {method!r}")
        failures = []
        for channel in (("email", "telegram") if method == "both" else (method,)):
            try:
                if channel == "email":
                    self.send_email(full_message, portfolio_summary)
                else:
                    self.send_telegram(full_message)
            except Exception as e:
                failures.append(f"{channel}: {e}")
        if failures:
            raise ReportDeliveryError("Report delivery failed: " + "; ".join(failures))

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

        fallback_banner = ""
        if portfolio_summary and portfolio_summary.get("data_source") == "csv_fallback":
            fallback_banner = (
                '<div style="background: #fff3cd; color: #856404; border: 1px solid '
                '#ffeeba; border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; '
                'font-size: 13px;">Live IBKR sync failed — positions come from the '
                "last saved snapshot and may be stale.</div>"
            )

        # Build risk summary table if portfolio_summary is available
        risk_table = ""
        if portfolio_summary and portfolio_summary.get("stocks"):
            composite = portfolio_summary.get("portfolio_composite_score", 0)
            rating = portfolio_summary.get("portfolio_rating", "green")
            badge_color = RATING_COLORS.get(rating, MUTED_COLOR)
            composite_label = "Portfolio Composite"
            if portfolio_summary.get("data_complete") is False:
                badge_color = MUTED_COLOR
                composite_label += " (provisional)"

            rows = ""
            for stock in portfolio_summary["stocks"]:
                s_rating = stock["composite"]["rating"]
                s_color = RATING_COLORS.get(s_rating, MUTED_COLOR)
                s_score = stock["composite"]["composite_score"]
                if not stock["composite"].get("data_complete", True):
                    s_color = MUTED_COLOR

                avg = stock.get("avg_price")
                has_avg = (
                    avg is not None
                    and not (isinstance(avg, float) and math.isnan(avg))
                    and avg > 0
                )
                entry_cell = f"{avg:,.2f}" if has_avg else "–"
                pnl = stock.get("unrealized_pnl_pct")
                if pnl is None:
                    pnl_cell = f'<span style="color: {MUTED_COLOR};">–</span>'
                else:
                    pnl_color = (
                        RATING_COLORS["green"]
                        if pnl > 0
                        else RATING_COLORS["red"] if pnl < 0 else MUTED_COLOR
                    )
                    pnl_cell = f'<span style="color: {pnl_color}; font-weight: bold;">{pnl:+.1f}%</span>'
                act = stock.get("action")
                trim = stock.get("trim_pct", 0)
                act_label = actions.action_label(act, trim, stock.get("position_size"))
                if act == "take_profit_candidate":
                    act_cell = f'<span style="color: {RATING_COLORS["green"]}; font-weight: bold;">{act_label}</span>'
                elif act == "cut_loss_candidate":
                    act_cell = f'<span style="color: {RATING_COLORS["red"]}; font-weight: bold;">{act_label}</span>'
                else:
                    act_cell = f'<span style="color: {MUTED_COLOR};">{act_label}</span>'

                rows += f"""
                <tr>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; font-weight: bold;">{html_mod.escape(stock["ticker"])}</td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: center;">
                        <span style="background: {s_color}; color: white; padding: 2px 10px; border-radius: 12px; font-size: 13px;">{s_score}/100</span>
                    </td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: right;">{stock["position_pct"]:.1f}%</td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: right;">{entry_cell}</td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: right;">{pnl_cell}</td>
                    <td style="padding: 8px 12px; border-bottom: 1px solid #eee; text-align: center;">{act_cell}</td>
                </tr>"""

            risk_table = f"""
            <div style="margin: 20px 0;">
                <div style="text-align: center; margin-bottom: 16px;">
                    <span style="font-size: 14px; color: #666;">{composite_label}</span><br>
                    <span style="background: {badge_color}; color: white; padding: 6px 20px; border-radius: 16px; font-size: 20px; font-weight: bold; display: inline-block; margin-top: 8px;">{composite}/100</span>
                </div>
                <table style="width: 100%; border-collapse: collapse; font-family: monospace;">
                    <tr style="background: #f8f9fa;">
                        <th style="padding: 8px 12px; text-align: left; border-bottom: 2px solid #ddd;">Ticker</th>
                        <th style="padding: 8px 12px; text-align: center; border-bottom: 2px solid #ddd;">Risk Score</th>
                        <th style="padding: 8px 12px; text-align: right; border-bottom: 2px solid #ddd;">Weight</th>
                        <th style="padding: 8px 12px; text-align: right; border-bottom: 2px solid #ddd;">Entry</th>
                        <th style="padding: 8px 12px; text-align: right; border-bottom: 2px solid #ddd;">P&amp;L</th>
                        <th style="padding: 8px 12px; text-align: center; border-bottom: 2px solid #ddd;">Candidate</th>
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
        {fallback_banner}
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
            raise ReportDeliveryError("Email is not configured")

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = config.EMAIL_FROM or smtp_user
            recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]
            if not recipients:
                raise ReportDeliveryError("No email recipients configured")
            msg["To"] = ", ".join(recipients)
            subject = f"Proteus Risk Report - {datetime.now().strftime('%Y-%m-%d')}"
            if portfolio_summary and portfolio_summary.get("data_source") == "csv_fallback":
                subject = "[CSV FALLBACK] " + subject
            if portfolio_summary and portfolio_summary.get("portfolio_red_alert"):
                subject = "[RISK ALERT] " + subject
            if portfolio_summary and portfolio_summary.get("data_complete") is False:
                subject = "[INCOMPLETE DATA] " + subject
            if portfolio_summary is None:
                subject = "[ANALYSIS FAILED] " + subject
            msg["Subject"] = subject

            # Plain-text fallback
            msg.attach(MIMEText(message, "plain", "utf-8"))

            # HTML version (preferred by email clients)
            html_body = self.format_html_report(message, portfolio_summary)
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                refused = server.sendmail(msg["From"], recipients, msg.as_string())
                if refused:
                    raise ReportDeliveryError("Email refused for one or more recipients")

            cprint("✅ Email Report Sent!", "green")
        except Exception as e:
            cprint(f"❌ Email Send Error: {e}", "red")
            raise

    def send_telegram(self, message):
        """Send report via Telegram"""
        bot_token = config.TELEGRAM_BOT_TOKEN
        chat_id = config.TELEGRAM_CHAT_ID

        if not bot_token or not chat_id:
            raise ReportDeliveryError("Telegram is not configured")

        async def _send(content):
            content = content.lstrip()
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
                    if split_idx <= 0:
                        split_idx = max_length

                    parts.append(content[:split_idx])
                    content = content[split_idx:].lstrip()

                for part in parts:
                    await bot.send_message(chat_id=chat_id, text=part)

                cprint("✅ Telegram Report Sent!", "green")
            except Exception as e:
                cprint(f"❌ Telegram Send Error: {e}", "red")
                raise

        try:
            asyncio.run(_send(message))
        except Exception as e:
            cprint(f"❌ Telegram Async Error: {e}", "red")
            raise

    def job(self, reraise=False):
        """Job to run on schedule"""
        cprint(f"⏰ Running scheduled analysis at {datetime.now()}", "cyan")
        try:
            self.analyze_portfolio()
        except Exception as e:
            # An uncaught exception here would kill the scheduler loop for good;
            # the one-shot systemd path passes reraise=True so a failed run
            # exits nonzero and the unit is marked failed instead of green
            cprint(f"❌ Analysis run failed: {e}", "red")
            if not isinstance(e, ReportDeliveryError) and not getattr(e, "report_sent", False):
                try:
                    self.send_report(f"ANALYSIS UNAVAILABLE\n{e}\nNo reliable portfolio score or keep/trim recommendation was produced. Check the data and rerun.")
                except Exception as delivery_error:
                    cprint(f"❌ Failure notice could not be delivered: {delivery_error}", "red")
            if reraise:
                raise

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
    import sys

    agent = StockRiskAgent()
    if "--loop" in sys.argv:
        agent.run()  # internal scheduler (Docker deployment)
    else:
        agent.job(reraise=True)  # one-shot (systemd timer deployment)
