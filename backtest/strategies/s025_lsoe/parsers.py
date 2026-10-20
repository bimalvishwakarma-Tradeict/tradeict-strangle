"""Parse public Delta India / Deribit option snapshots into sqlite rows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
DELTA_EXPIRY_HOUR_IST = 17
DELTA_EXPIRY_MINUTE_IST = 30
DERIBIT_EXPIRY_HOUR_UTC = 8

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def parse_delta_symbol(symbol: str) -> tuple[str | None, float | None, datetime | None]:
    """C-BTC-100000-201026 → call, 100000, 2026-10-20 17:30 IST."""
    parts = str(symbol or "").upper().split("-")
    if len(parts) < 4:
        return None, None, None
    kind_code = parts[0]
    if kind_code not in ("C", "P"):
        return None, None, None
    opt_type = "call" if kind_code == "C" else "put"
    strike = safe_float(parts[2])
    ddmmyy = parts[3]
    if len(ddmmyy) != 6 or not ddmmyy.isdigit():
        return opt_type, strike, None
    day = int(ddmmyy[0:2])
    month = int(ddmmyy[2:4])
    year = 2000 + int(ddmmyy[4:6])
    try:
        expiry_ist = datetime(
            year,
            month,
            day,
            DELTA_EXPIRY_HOUR_IST,
            DELTA_EXPIRY_MINUTE_IST,
            0,
            tzinfo=IST,
        )
    except ValueError:
        return opt_type, strike, None
    return opt_type, strike, expiry_ist.astimezone(UTC)


def parse_deribit_instrument(
    name: str,
) -> tuple[str | None, float | None, datetime | None]:
    """BTC-20OCT26-100000-C → call, 100000, 2026-10-20 08:00 UTC."""
    parts = str(name or "").upper().split("-")
    if len(parts) < 4:
        return None, None, None
    kind_code = parts[-1]
    if kind_code not in ("C", "P"):
        return None, None, None
    opt_type = "call" if kind_code == "C" else "put"
    strike = safe_float(parts[-2])
    date_token = parts[1]
    expiry = _parse_deribit_date(date_token)
    return opt_type, strike, expiry


def _parse_deribit_date(token: str) -> datetime | None:
    token = token.upper()
    # 20OCT26 or 27MAR26
    if len(token) < 7:
        return None
    day_s = token[:-5]
    mon_s = token[-5:-2]
    yy_s = token[-2:]
    if mon_s not in _MONTHS or not day_s.isdigit() or not yy_s.isdigit():
        return None
    try:
        return datetime(
            2000 + int(yy_s),
            _MONTHS[mon_s],
            int(day_s),
            DERIBIT_EXPIRY_HOUR_UTC,
            0,
            0,
            tzinfo=UTC,
        )
    except ValueError:
        return None


def expiry_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_row(ts: str, src: str, symbol: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "src": src,
        "symbol": symbol,
        "strike": None,
        "expiry": None,
        "type": None,
        "oi": None,
        "mark": None,
        "mark_iv": None,
        "bid": None,
        "ask": None,
        "bid_iv": None,
        "ask_iv": None,
        "delta": None,
        "gamma": None,
        "underlying": None,
    }


def delta_ticker_to_row(ts: str, ticker: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(ticker.get("symbol") or "")
    if not symbol:
        return None
    contract = str(ticker.get("contract_type") or "").lower()
    is_option = contract in ("call_options", "put_options") or symbol[:2] in (
        "C-",
        "P-",
    )
    row = _empty_row(ts, "delta", symbol)
    if is_option:
        opt_type, strike, expiry = parse_delta_symbol(symbol)
        row["type"] = opt_type or (
            "call" if "call" in contract else "put" if "put" in contract else None
        )
        row["strike"] = strike if strike is not None else safe_float(ticker.get("strike"))
        row["expiry"] = expiry_iso(expiry)
    else:
        row["type"] = "perp" if "perpetual" in contract or symbol.endswith("USD") else contract or None

    quotes = ticker.get("quotes") if isinstance(ticker.get("quotes"), dict) else {}
    greeks = ticker.get("greeks") if isinstance(ticker.get("greeks"), dict) else {}
    row["oi"] = safe_float(
        ticker.get("open_interest")
        if ticker.get("open_interest") is not None
        else ticker.get("oi")
    )
    row["mark"] = safe_float(ticker.get("mark_price") or ticker.get("close"))
    row["mark_iv"] = safe_float(
        ticker.get("mark_vol")
        if ticker.get("mark_vol") is not None
        else ticker.get("mark_iv")
    )
    row["bid"] = safe_float(
        quotes.get("best_bid")
        if quotes.get("best_bid") is not None
        else ticker.get("best_bid")
    )
    row["ask"] = safe_float(
        quotes.get("best_ask")
        if quotes.get("best_ask") is not None
        else ticker.get("best_ask")
    )
    row["bid_iv"] = safe_float(
        quotes.get("bid_iv")
        if quotes.get("bid_iv") is not None
        else ticker.get("bid_iv")
    )
    row["ask_iv"] = safe_float(
        quotes.get("ask_iv")
        if quotes.get("ask_iv") is not None
        else ticker.get("ask_iv")
    )
    row["delta"] = safe_float(greeks.get("delta") if greeks else ticker.get("delta"))
    row["gamma"] = safe_float(greeks.get("gamma") if greeks else ticker.get("gamma"))
    row["underlying"] = safe_float(
        ticker.get("spot_price")
        or ticker.get("underlying_price")
        or ticker.get("close")
        or _nested(ticker, "spot_price")
    )
    return row


def deribit_summary_to_row(ts: str, item: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(item.get("instrument_name") or "")
    if not symbol:
        return None
    opt_type, strike, expiry = parse_deribit_instrument(symbol)
    row = _empty_row(ts, "deribit", symbol)
    row["type"] = opt_type
    row["strike"] = strike
    row["expiry"] = expiry_iso(expiry)
    row["oi"] = safe_float(item.get("open_interest"))
    row["mark"] = safe_float(item.get("mark_price"))
    row["mark_iv"] = safe_float(item.get("mark_iv"))
    row["bid"] = safe_float(item.get("bid_price"))
    row["ask"] = safe_float(item.get("ask_price"))
    row["bid_iv"] = safe_float(item.get("bid_iv"))
    row["ask_iv"] = safe_float(item.get("ask_iv"))
    greeks = item.get("greeks") if isinstance(item.get("greeks"), dict) else {}
    row["delta"] = safe_float(greeks.get("delta") if greeks else item.get("delta"))
    row["gamma"] = safe_float(greeks.get("gamma") if greeks else item.get("gamma"))
    row["underlying"] = safe_float(item.get("underlying_price"))
    return row


SNAPSHOT_COLUMNS = (
    "ts",
    "src",
    "symbol",
    "strike",
    "expiry",
    "type",
    "oi",
    "mark",
    "mark_iv",
    "bid",
    "ask",
    "bid_iv",
    "ask_iv",
    "delta",
    "gamma",
    "underlying",
)
