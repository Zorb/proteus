"""
portfolio_sync.py — Fetch portfolio from broker APIs.

Supports:
- Interactive Brokers (Flex Query API — XML, two-step request/poll)
- Trading 212 (REST API — JSON, single GET request)

Both return a DataFrame with columns: Ticker, Position_Size, Avg_Price
matching the portfolio.csv schema. Returns None on any failure.
"""

import time
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import pandas as pd
from termcolor import cprint
import config


FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"

# Map IBKR listing exchanges to Yahoo Finance ticker suffixes.
# US exchanges (NYSE, NASDAQ, ARCA, AMEX, BATS, etc.) get no suffix.
EXCHANGE_SUFFIX = {
    "LSE": ".L",  # London Stock Exchange
    "LSEETF": ".L",  # London ETFs
    "TSE": ".TO",  # Toronto Stock Exchange
    "VSE": ".V",  # TSX Venture
    "ASX": ".AX",  # Australian Securities Exchange
    "FRA": ".F",  # Frankfurt
    "IBIS": ".DE",  # XETRA (Germany)
    "SWX": ".SW",  # Swiss Exchange
    "PAR": ".PA",  # Euronext Paris
    "AMS": ".AS",  # Euronext Amsterdam
    "BRU": ".BR",  # Euronext Brussels
    "LIS": ".LS",  # Euronext Lisbon
    "MIL": ".MI",  # Borsa Italiana (Milan)
    "BME": ".MC",  # Bolsa de Madrid
    "OSE": ".OL",  # Oslo Stock Exchange
    "STO": ".ST",  # Stockholm (Nasdaq Nordic)
    "HEL": ".HE",  # Helsinki (Nasdaq Nordic)
    "CPH": ".CO",  # Copenhagen (Nasdaq Nordic)
    "KSE": ".KS",  # Korea Stock Exchange
    "HKSE": ".HK",  # Hong Kong Stock Exchange
    "SGX": ".SI",  # Singapore Exchange
    "TSE.JPN": ".T",  # Tokyo Stock Exchange
    "NSE": ".NS",  # National Stock Exchange (India)
    "BSE": ".BO",  # Bombay Stock Exchange
    "JSE": ".JO",  # Johannesburg Stock Exchange
    "BOVESPA": ".SA",  # Brazil
    "BMV": ".MX",  # Mexico
    "WSE": ".WA",  # Warsaw Stock Exchange
}


def fetch_ibkr_portfolio():
    """
    Fetch current open positions from IBKR via Flex Query API.

    Requires config.IBKR_FLEX_TOKEN and config.IBKR_FLEX_QUERY_ID to be set.

    Returns:
        pd.DataFrame with columns [Ticker, Position_Size, Avg_Price] or None on failure.
    """
    token = config.IBKR_FLEX_TOKEN
    query_id = config.IBKR_FLEX_QUERY_ID

    if not token or not query_id:
        cprint("⚠️ IBKR Flex Query credentials not configured", "yellow")
        return None

    # Step 1: Send the request
    reference_code = _send_request(token, query_id)
    if reference_code is None:
        return None

    # Step 2: Wait for the statement to be generated
    cprint("  ⏳ Waiting for Flex statement to generate...", "cyan")
    time.sleep(5)

    # Step 3: Retrieve the statement (with retries for "not yet available")
    xml_data = _get_statement(token, reference_code)
    if xml_data is None:
        return None

    # Step 4: Parse positions from XML
    positions = _parse_positions(xml_data)
    if not positions:
        cprint("⚠️ No stock positions found in IBKR response", "yellow")
        return None

    df = pd.DataFrame(positions, columns=["Ticker", "Position_Size", "Avg_Price"])
    return df


def _send_request(token, query_id):
    """Step 1: Submit the Flex Query and get a ReferenceCode."""
    url = f"{FLEX_BASE_URL}/SendRequest?t={token}&q={query_id}&v=3"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")

        root = ET.fromstring(body)

        status = root.findtext("Status")
        if status != "Success":
            error_msg = root.findtext("ErrorMessage", "Unknown error")
            cprint(f"  ❌ IBKR SendRequest failed: {error_msg}", "red")
            return None

        reference_code = root.findtext("ReferenceCode")
        if not reference_code:
            cprint("  ❌ IBKR SendRequest returned no ReferenceCode", "red")
            return None

        cprint(f"  ✅ Flex Query submitted (ref: {reference_code})", "green")
        return reference_code

    except Exception as e:
        cprint(f"  ❌ IBKR SendRequest error: {e}", "red")
        return None


def _get_statement(token, reference_code, max_attempts=5, delay=5):
    """Step 2: Retrieve the Flex statement XML. Retries if not yet ready."""
    url = f"{FLEX_BASE_URL}/GetStatement?t={token}&q={reference_code}&v=3"

    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")

            # Check if it's an error/status XML (short response)
            if body.strip().startswith("<FlexStatementResponse"):
                root = ET.fromstring(body)
                status = root.findtext("Status")
                error_msg = root.findtext("ErrorMessage", "")

                if "try again" in error_msg.lower() or status != "Success":
                    if attempt < max_attempts:
                        cprint(
                            f"  ⏳ Statement not ready (attempt {attempt}/{max_attempts}), retrying in {delay}s...",
                            "yellow",
                        )
                        time.sleep(delay)
                        continue
                    else:
                        cprint(
                            f"  ❌ IBKR statement not ready after {max_attempts} attempts",
                            "red",
                        )
                        return None

            # If we get here, it should be the full FlexQueryResponse XML
            return body

        except Exception as e:
            if attempt < max_attempts:
                cprint(
                    f"  ⚠️ IBKR GetStatement attempt {attempt}/{max_attempts} failed: {e}. Retrying in {delay}s...",
                    "yellow",
                )
                time.sleep(delay)
            else:
                cprint(
                    f"  ❌ IBKR GetStatement failed after {max_attempts} attempts: {e}",
                    "red",
                )
                return None

    return None


def _parse_positions(xml_data):
    """Parse OpenPosition elements from the Flex statement XML."""
    positions = []

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        cprint(f"  ❌ Failed to parse IBKR XML: {e}", "red")
        return positions

    # Flex Query XML structure: FlexQueryResponse > FlexStatements > FlexStatement > OpenPositions > OpenPosition
    for position in root.iter("OpenPosition"):
        asset_class = position.get("assetCategory", "")

        # Only include stocks (skip options, futures, forex, bonds, warrants, etc.)
        if asset_class != "STK":
            continue

        symbol = position.get("symbol", "").strip()
        if not symbol:
            continue

        # Get quantity (absolute value — Flex reports short positions as negative)
        try:
            quantity = abs(float(position.get("position", "0")))
        except ValueError:
            continue

        if quantity == 0:
            continue

        # Get cost basis per share
        try:
            cost_basis = float(position.get("costBasisPrice", "0"))
        except ValueError:
            cost_basis = 0.0

        # Map exchange to Yahoo Finance suffix
        exchange = position.get("listingExchange", "")
        suffix = EXCHANGE_SUFFIX.get(exchange, "")
        ticker = f"{symbol}{suffix}"

        positions.append(
            {
                "Ticker": ticker,
                "Position_Size": quantity,
                "Avg_Price": round(cost_basis, 4),
            }
        )

    cprint(f"  📋 Parsed {len(positions)} stock positions from IBKR", "cyan")
    return positions


# ── Trading 212 ──────────────────────────────────────────────

T212_BASE_URL = "https://live.trading212.com"

# Map Trading 212 country codes (from ticker format SYMBOL_CC_EQ) to Yahoo Finance suffixes.
# US stocks get no suffix.
T212_COUNTRY_SUFFIX = {
    "US": "",
    "GB": ".L",       # London Stock Exchange
    "DE": ".DE",      # XETRA (Germany)
    "FR": ".PA",      # Euronext Paris
    "NL": ".AS",      # Euronext Amsterdam
    "BE": ".BR",      # Euronext Brussels
    "PT": ".LS",      # Euronext Lisbon
    "IT": ".MI",      # Borsa Italiana (Milan)
    "ES": ".MC",      # Bolsa de Madrid
    "CH": ".SW",      # Swiss Exchange
    "SE": ".ST",      # Stockholm (Nasdaq Nordic)
    "NO": ".OL",      # Oslo Stock Exchange
    "DK": ".CO",      # Copenhagen (Nasdaq Nordic)
    "FI": ".HE",      # Helsinki (Nasdaq Nordic)
    "CA": ".TO",      # Toronto Stock Exchange
    "AU": ".AX",      # Australian Securities Exchange
    "HK": ".HK",      # Hong Kong Stock Exchange
    "SG": ".SI",      # Singapore Exchange
    "JP": ".T",       # Tokyo Stock Exchange
    "KR": ".KS",      # Korea Stock Exchange
    "IN": ".NS",      # National Stock Exchange (India)
    "ZA": ".JO",      # Johannesburg Stock Exchange
    "BR": ".SA",      # Brazil
    "MX": ".MX",      # Mexico
    "PL": ".WA",      # Warsaw Stock Exchange
    "AT": ".VI",      # Vienna Stock Exchange
    "IE": ".IR",      # Irish Stock Exchange
}


def _t212_ticker_to_yahoo(t212_ticker):
    """
    Convert Trading 212 ticker format (e.g. 'AAPL_US_EQ') to Yahoo Finance format ('AAPL').

    T212 format: SYMBOL_COUNTRY_TYPE (e.g. AAPL_US_EQ, LGEN_GB_EQ, SAP_DE_EQ)
    """
    parts = t212_ticker.rsplit("_", 2)
    if len(parts) == 3:
        symbol, country, _ = parts
    elif len(parts) == 2:
        symbol, country = parts[0], parts[1]
    else:
        symbol, country = t212_ticker, "US"

    suffix = T212_COUNTRY_SUFFIX.get(country, "")
    return f"{symbol}{suffix}"


def fetch_t212_portfolio():
    """
    Fetch current open positions from Trading 212 via REST API.

    Requires config.T212_API_KEY to be set.

    Returns:
        pd.DataFrame with columns [Ticker, Position_Size, Avg_Price] or None on failure.
    """
    api_key = config.T212_API_KEY

    if not api_key:
        cprint("⚠️ Trading 212 API key not configured", "yellow")
        return None

    url = f"{T212_BASE_URL}/api/v0/equity/portfolio"

    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", api_key)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")

        data = json.loads(body)

        if not data:
            cprint("⚠️ No positions found in Trading 212 response", "yellow")
            return None

        positions = []
        for pos in data:
            t212_ticker = pos.get("ticker", "")
            quantity = pos.get("quantity", 0)
            avg_price = pos.get("averagePrice", 0)

            if not t212_ticker or quantity == 0:
                continue

            yahoo_ticker = _t212_ticker_to_yahoo(t212_ticker)
            positions.append({
                "Ticker": yahoo_ticker,
                "Position_Size": abs(quantity),
                "Avg_Price": round(avg_price, 4),
            })

        if not positions:
            cprint("⚠️ No equity positions found in Trading 212 response", "yellow")
            return None

        cprint(f"  📋 Parsed {len(positions)} positions from Trading 212", "cyan")
        df = pd.DataFrame(positions)
        return df

    except Exception as e:
        cprint(f"  ❌ Trading 212 API error: {e}", "red")
        return None
