"""
portfolio_sync.py — Fetch portfolio from Interactive Brokers via Flex Queries.

Two-step API flow:
1. SendRequest — submit the query, get a ReferenceCode
2. GetStatement — retrieve the results as XML (after a short delay)

Returns a DataFrame with Ticker, Position_Size, Avg_Price and Currency (USD).
An empty DataFrame is a valid empty stock portfolio. None indicates an unavailable
response; invalid or unsupported holdings raise PortfolioValidationError.
"""

import time
import math
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import pandas as pd
from termcolor import cprint
import config


FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"


class PortfolioValidationError(ValueError):
    """Holdings cannot safely be interpreted as a long-only USD portfolio."""


def normalize_positions(df):
    """Validate CSV/broker holdings and combine lots using quantity-weighted cost.

    Legacy CSVs without Currency use the account's explicit USD convention.
    Missing entry prices remain unknown; they are never used as market prices.
    """
    required = {"Ticker", "Position_Size", "Avg_Price"}
    if not required.issubset(df.columns):
        raise PortfolioValidationError("Portfolio requires Ticker, Position_Size and Avg_Price")
    groups = {}
    for _, row in df.iterrows():
        ticker = row["Ticker"]
        if not isinstance(ticker, str) or not ticker.strip():
            raise PortfolioValidationError("Portfolio contains a missing ticker")
        ticker = ticker.strip().upper()
        try:
            quantity = float(row["Position_Size"])
        except (TypeError, ValueError):
            raise PortfolioValidationError(f"{ticker}: invalid position size")
        if not math.isfinite(quantity) or quantity < 0:
            raise PortfolioValidationError(f"{ticker}: position must be finite and long-only")
        if quantity == 0:
            continue
        currency = row.get("Currency", "USD")
        if currency != "USD":
            raise PortfolioValidationError(f"{ticker}: expected USD, received {currency!r}")
        try:
            avg = float(row["Avg_Price"])
        except (TypeError, ValueError):
            avg = float("nan")
        avg = avg if math.isfinite(avg) and avg > 0 else None
        group = groups.setdefault(ticker, {"quantity": 0.0, "cost": 0.0, "entry_known": True})
        group["quantity"] += quantity
        if avg is None:
            group["entry_known"] = False
        else:
            group["cost"] += quantity * avg
    rows = []
    for ticker, group in groups.items():
        if not math.isfinite(group["quantity"]) or not math.isfinite(group["cost"]):
            raise PortfolioValidationError(f"{ticker}: position totals overflowed")
        rows.append({
            "Ticker": ticker,
            "Position_Size": group["quantity"],
            "Avg_Price": group["cost"] / group["quantity"] if group["entry_known"] else None,
            "Currency": "USD",
        })
    return pd.DataFrame(rows, columns=["Ticker", "Position_Size", "Avg_Price", "Currency"])

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
        Validated USD holdings, including an empty DataFrame for zero positions.
        None on transport/response failure; invalid holdings raise explicitly.
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
    if positions is None:
        return None

    # [] is a valid empty portfolio, not an unavailable broker response.
    df = pd.DataFrame(positions, columns=["Ticker", "Position_Size", "Avg_Price", "Currency"])
    return normalize_positions(df)


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
    """Return positions, [] for a verified empty statement, or None for bad XML.

    Summary rows take precedence over lots for the same account/instrument.
    Ambiguous holdings fail explicitly instead of silently reusing old holdings.
    """
    positions = []

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        cprint(f"  ❌ Failed to parse IBKR XML: {e}", "red")
        return None

    statements = root.findall("./FlexStatements/FlexStatement")
    if root.tag != "FlexQueryResponse" or not statements or any(
        statement.find("OpenPositions") is None for statement in statements
    ):
        cprint("  ❌ IBKR response is missing its OpenPositions section", "red")
        return None

    grouped = {}
    report_dates = set()
    for statement in statements:
        for position in statement.findall("./OpenPositions/OpenPosition"):
            if not position.get("assetCategory"):
                raise PortfolioValidationError("IBKR position is missing AssetCategory")
            if position.get("assetCategory") != "STK":
                continue
            report_date = position.get("reportDate") or statement.get("toDate")
            if report_date:
                report_dates.add(report_date)
            key = (
                position.get("accountId", statement.get("accountId", "")),
                position.get("conid") or position.get("symbol", ""),
                position.get("listingExchange", ""),
            )
            grouped.setdefault(key, []).append(position)

    if len(report_dates) > 1:
        raise PortfolioValidationError("IBKR positions span multiple report dates")

    selected = []
    for rows in grouped.values():
        if any(row.get("levelOfDetail", "SUMMARY").upper() not in ("SUMMARY", "LOT") for row in rows):
            raise PortfolioValidationError("IBKR returned an unsupported position detail level")
        summaries = [row for row in rows if row.get("levelOfDetail", "SUMMARY").upper() == "SUMMARY"]
        if len(summaries) > 1:
            raise PortfolioValidationError("IBKR returned duplicate summaries for an instrument/account")
        selected.extend(summaries or rows)

    # Flex Query XML structure: FlexQueryResponse > FlexStatements > FlexStatement > OpenPositions > OpenPosition
    for position in selected:
        asset_class = position.get("assetCategory", "")

        # Only include stocks (skip options, futures, forex, bonds, warrants, etc.)
        if asset_class != "STK":
            continue

        symbol = position.get("symbol", "").strip()
        if not symbol:
            raise PortfolioValidationError("IBKR stock position has no symbol")

        # Preserve direction so unsupported shorts fail validation.
        try:
            quantity = float(position.get("position", "nan"))
        except ValueError:
            raise PortfolioValidationError(f"{symbol}: invalid broker quantity")

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
                "Currency": position.get("currency", ""),
            }
        )

    cprint(f"  📋 Parsed {len(positions)} stock positions from IBKR", "cyan")
    return positions
