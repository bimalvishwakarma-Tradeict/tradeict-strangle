# test_s003_indicators.py — pure indicator tests (no pandas/numpy)

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from backend.strategies.s003_lsr4.indicators import (
    ATR,
    DMI_ADX,
    RMA,
    RSI,
    SMA,
    SessionVWAP,
)

FIXTURE = Path(__file__).parent / "fixtures" / "s003_candles_300.csv"


def _load_fixture() -> list[dict]:
    rows: list[dict] = []
    with FIXTURE.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(
                {
                    "open_time": datetime.fromisoformat(
                        row["open_time"].replace("Z", "+00:00")
                    ),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
    assert len(rows) == 300
    return rows


def test_rma_seeds_with_sma_not_first_value() -> None:
    """Pine ta.rma seeds with SMA(length); seeding from first value diverges."""
    seq = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    rma = RMA(5)
    outs = [rma.update(x) for x in seq]
    assert outs[:4] == [None, None, None, None]
    assert outs[4] == pytest.approx(3.0)  # SMA(1..5)
    assert outs[5] == pytest.approx(3.6)  # 3 + (6-3)/5
    assert outs[6] == pytest.approx(4.28)  # 3.6 + (7-3.6)/5

    # Wrong seed (first value) would give different path
    wrong = 1.0
    for x in seq[1:]:
        wrong = wrong + (x - wrong) / 5.0
    assert wrong != pytest.approx(outs[6])


def test_sma_warmup() -> None:
    s = SMA(3)
    assert s.update(1.0) is None
    assert s.update(2.0) is None
    assert s.update(3.0) == pytest.approx(2.0)
    assert s.update(6.0) == pytest.approx(3.6666667)


def test_fixture_atr_rsi_adx_vwap_to_2dp() -> None:
    rows = _load_fixture()
    atr = ATR(14)
    rsi = RSI(14)
    dmi = DMI_ADX(14)
    vwap = SessionVWAP("Asia/Kolkata")
    prev = None
    vals: list[tuple] = []
    for r in rows:
        a = (
            atr.update(r["high"], r["low"], prev)
            if prev is not None
            else None
        )
        rs = rsi.update(r["close"])
        d = dmi.update(r["high"], r["low"], r["close"])
        vw = vwap.update(
            r["open_time"], r["high"], r["low"], r["close"], r["volume"]
        )
        vals.append((a, rs, d[2] if d else None, vw))
        prev = r["close"]

    assert vwap.session_boundary_crossed is True

    # Golden values locked from fixture (2 d.p. contract)
    a, rs, adx, vw = vals[199]
    assert round(a, 2) == 5.66
    assert round(rs, 2) == 53.80
    assert round(adx, 2) == 94.01
    assert round(vw, 2) == 100025.35

    a, rs, adx, vw = vals[299]
    assert round(a, 2) == 5.66
    assert round(rs, 2) == 53.81
    assert round(adx, 2) == 93.01
    assert round(vw, 2) == 100032.99


def test_session_vwap_resets_on_ist_date_change() -> None:
    v = SessionVWAP("Asia/Kolkata")
    t1 = datetime.fromisoformat("2025-01-14T18:30:00+00:00")  # 00:00 IST 15th? 
    # 14 Jan 22:00 IST = 14 Jan 16:30 UTC
    t_before = datetime.fromisoformat("2025-01-14T16:30:00+00:00")
    t_after = datetime.fromisoformat("2025-01-14T18:30:00+00:00")  # 00:00 IST 15th
    v1 = v.update(t_before, 10, 8, 9, 100)
    assert v.session_boundary_crossed is False
    v2 = v.update(t_after, 20, 18, 19, 100)
    assert v.session_boundary_crossed is True
    # New session VWAP should equal hlc3 of the new bar alone (vol=100)
    assert v2 == pytest.approx((20 + 18 + 19) / 3.0)
    assert v1 != v2
