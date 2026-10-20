"""Delta / Deribit sample JSON → sqlite-shaped rows; expiry 17:30 IST / 08:00 UTC."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backtest.strategies.s025_lsoe.parsers import (
    IST,
    UTC,
    delta_ticker_to_row,
    deribit_summary_to_row,
    parse_delta_symbol,
    parse_deribit_instrument,
)

SAMPLE_DELTA = {
    "symbol": "C-BTC-100000-201026",
    "contract_type": "call_options",
    "strike": "100000",
    "mark_price": "1234.5",
    "spot_price": "108250.0",
    "open_interest": "42.0",
    "mark_vol": "0.55",
    "best_bid": "1200",
    "best_ask": "1260",
    "greeks": {"delta": "0.41", "gamma": "0.00012"},
    "quotes": {
        "best_bid": "1201",
        "best_ask": "1259",
        "bid_iv": "0.52",
        "ask_iv": "0.58",
        "bid_size": "15",
        "ask_size": "12",
    },
}

SAMPLE_DELTA_PUT = {
    "symbol": "P-BTC-95000-201026",
    "contract_type": "put_options",
    "mark_price": "890.25",
    "open_interest": "10",
    "spot_price": "108250.0",
    "greeks": {"delta": "-0.22", "gamma": "0.00009"},
    "quotes": {"best_bid": "880", "best_ask": "900", "bid_iv": "0.60", "ask_iv": "0.64"},
}

SAMPLE_DERIBIT = {
    "instrument_name": "BTC-20OCT26-100000-C",
    "open_interest": 125.5,
    "mark_iv": 48.2,
    "bid_price": 0.012,
    "ask_price": 0.013,
    "mark_price": 0.0124,
    "underlying_price": 108200.0,
    "greeks": {"delta": 0.39, "gamma": 0.00008},
}

SAMPLE_DERIBIT_PUT = {
    "instrument_name": "BTC-20OCT26-95000-P",
    "open_interest": 80,
    "mark_iv": 51.0,
    "bid_price": 0.009,
    "ask_price": 0.01,
    "mark_price": 0.0094,
    "underlying_price": 108200.0,
    "delta": -0.18,
    "gamma": 0.00007,
}

TS = "2026-10-20T06:00:00Z"


def test_delta_symbol_expiry_1730_ist() -> None:
    opt_type, strike, expiry = parse_delta_symbol("C-BTC-100000-201026")
    assert opt_type == "call"
    assert strike == 100000.0
    assert expiry is not None
    ist = expiry.astimezone(IST)
    assert ist.hour == 17
    assert ist.minute == 30
    assert expiry.astimezone(UTC).hour == 12  # 17:30 IST = 12:00 UTC


def test_deribit_instrument_expiry_0800_utc() -> None:
    opt_type, strike, expiry = parse_deribit_instrument("BTC-20OCT26-100000-C")
    assert opt_type == "call"
    assert strike == 100000.0
    assert expiry is not None
    assert expiry.tzinfo is not None
    utc = expiry.astimezone(UTC)
    assert utc.hour == 8
    assert utc.minute == 0
    assert utc.date() == datetime(2026, 10, 20).date()


def test_delta_call_row_units_and_quotes() -> None:
    row = delta_ticker_to_row(TS, SAMPLE_DELTA)
    assert row is not None
    assert row["src"] == "delta"
    assert row["symbol"] == "C-BTC-100000-201026"
    assert row["type"] == "call"
    assert row["strike"] == 100000.0
    assert row["oi"] == 42.0
    assert row["mark"] == 1234.5
    assert row["mark_iv"] == 0.55
    assert row["bid"] == 1201.0  # quotes override top-level
    assert row["ask"] == 1259.0
    assert row["bid_iv"] == 0.52
    assert row["ask_iv"] == 0.58
    assert row["delta"] == 0.41
    assert row["gamma"] == 0.00012
    assert row["underlying"] == 108250.0
    exp = datetime.fromisoformat(str(row["expiry"]).replace("Z", "+00:00"))
    assert exp.astimezone(IST).strftime("%H:%M") == "17:30"
    assert exp.astimezone(ZoneInfo("UTC")).hour == 12


def test_delta_put_row() -> None:
    row = delta_ticker_to_row(TS, SAMPLE_DELTA_PUT)
    assert row is not None
    assert row["type"] == "put"
    assert row["strike"] == 95000.0
    assert row["delta"] == -0.22


def test_deribit_call_row() -> None:
    row = deribit_summary_to_row(TS, SAMPLE_DERIBIT)
    assert row is not None
    assert row["src"] == "deribit"
    assert row["type"] == "call"
    assert row["strike"] == 100000.0
    assert row["oi"] == 125.5
    assert row["mark"] == 0.0124  # Deribit BTC units, stored as given
    assert row["mark_iv"] == 48.2
    assert row["bid"] == 0.012
    assert row["ask"] == 0.013
    assert row["underlying"] == 108200.0
    exp = datetime.fromisoformat(str(row["expiry"]).replace("Z", "+00:00"))
    assert exp.astimezone(UTC).strftime("%H:%M") == "08:00"


def test_deribit_put_row() -> None:
    row = deribit_summary_to_row(TS, SAMPLE_DERIBIT_PUT)
    assert row is not None
    assert row["type"] == "put"
    assert row["strike"] == 95000.0
    assert row["delta"] == -0.18
    assert row["gamma"] == 0.00007
