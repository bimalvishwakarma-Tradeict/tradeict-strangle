"""Handmade OBH/OBL rule tests + Wilder RSI sample."""

from __future__ import annotations

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import detect_from_rsi, rsi_wilder


def _bars(rows: list[tuple[float, float, float, float]]) -> tuple[np.ndarray, ...]:
    """rows: (rsi, o, h, l, c) wait packed as (rsi, h, l, c)."""
    n = len(rows)
    ts = np.arange(n, dtype=np.int64) * 60
    rsi = np.array([r[0] for r in rows], dtype=np.float64)
    h = np.array([r[1] for r in rows], dtype=np.float64)
    l = np.array([r[2] for r in rows], dtype=np.float64)
    c = np.array([r[3] for r in rows], dtype=np.float64)
    o = c.copy()
    return ts, o, h, l, c, rsi


def _run(rows: list[tuple[float, float, float, float]]):
    ts, o, h, l, c, rsi = _bars(rows)
    return detect_from_rsi(ts, o, h, l, c, rsi, 60, 70.0, 30.0)


def test_rsi_wilder_n2_known() -> None:
    close = np.array([1.0, 2.0, 3.0, 2.0], dtype=np.float64)
    rsi = rsi_wilder(close, 2)
    assert np.isnan(rsi[0]) and np.isnan(rsi[1])
    assert abs(float(rsi[2]) - 100.0) < 1e-9
    assert abs(float(rsi[3]) - 50.0) < 1e-9


def test_rule1_2_3_obh_form_confirm() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (71.0, 12.0, 10.0, 11.0),
        (75.0, 15.0, 11.0, 14.0),
        (72.0, 14.0, 12.0, 13.0),
        (65.0, 13.0, 10.0, 11.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert len(zones) == 0
    # still confirmed, not ended — inspect by firing expire
    rows2 = rows + [(20.0, 10.0, 8.0, 9.0)]
    sigs, zones = _run(rows2)
    assert sigs == []
    assert len(zones) == 1
    z = zones[0]
    assert z["kind"] == "OBH"
    assert z["level"] == 15.0
    assert z["rsi"] == 75.0
    assert z["end_reason"] == "rsi_below_exp"


def test_rule4_short_signal() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (71.0, 12.0, 10.0, 11.0),
        (80.0, 20.0, 11.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (55.0, 22.0, 12.0, 21.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["side"] == "short"
    assert s["ob_level"] == 20.0
    assert s["ob_rsi"] == 80.0
    assert s["sig_rsi"] == 55.0
    assert s["ts"] == 4 * 60 + 60
    assert zones[0]["end_reason"] == "signal"


def test_rule5_wick_above_obh_unchanged() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (71.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (55.0, 25.0, 10.0, 19.0),
        (50.0, 19.0, 10.0, 11.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert zones == []


def test_rule6_obh_expire_rsi_below_40() -> None:
    """Rev4: confirmed OBH expires on RSI < 40 (was < 30 / opposite_extreme)."""
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (71.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (25.0, 15.0, 8.0, 9.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert len(zones) == 1
    assert zones[0]["end_reason"] == "rsi_below_exp"


def test_rule7_new_obh_replaces_old() -> None:
    """Rev3: RSI>70 starts forming; replace happens when new confirms (RSI<=70)."""
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (71.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (72.0, 16.0, 10.0, 15.0),
        (55.0, 14.0, 9.0, 10.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert len(zones) == 1
    assert zones[0]["level"] == 20.0
    assert zones[0]["end_reason"] == "replaced"


def test_rule8_one_signal_per_obh() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (71.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (50.0, 22.0, 12.0, 21.0),
        (48.0, 24.0, 12.0, 23.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    assert zones[0]["end_reason"] == "signal"


def test_long_rules_1_3_obl_form_confirm() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (25.0, 18.0, 16.0, 17.0),
        (20.0, 17.0, 12.0, 13.0),
        (22.0, 16.0, 14.0, 15.0),
        (40.0, 18.0, 15.0, 16.0),
        (80.0, 20.0, 16.0, 19.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    z = [x for x in zones if x["kind"] == "OBL"]
    assert len(z) == 1
    assert z[0]["level"] == 12.0
    assert z[0]["rsi"] == 20.0
    assert z[0]["end_reason"] == "rsi_above_exp"


def test_long_rule4_signal() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (45.0, 12.0, 8.0, 9.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["side"] == "long"
    assert s["ob_level"] == 10.0
    assert s["ob_rsi"] == 20.0
    assert s["sig_rsi"] == 45.0
    assert zones[0]["end_reason"] == "signal"


def test_long_rule5_wick_below_unchanged() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (45.0, 15.0, 8.0, 11.0),
    ]
    sigs, _zones = _run(rows)
    assert sigs == []


def test_long_rule6_expire_rsi_above_60() -> None:
    """Rev4: confirmed OBL expires on RSI > 60 (was > 70 / opposite_extreme)."""
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (75.0, 18.0, 12.0, 17.0),
    ]
    _sigs, zones = _run(rows)
    assert any(z["kind"] == "OBL" and z["end_reason"] == "rsi_above_exp" for z in zones)


def test_long_rule7_replaced() -> None:
    """Rev3: RSI<30 starts forming; replace happens when new confirms (RSI>=30)."""
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (25.0, 15.0, 9.0, 10.0),
        (40.0, 16.0, 12.0, 13.0),
    ]
    _sigs, zones = _run(rows)
    assert len(zones) == 1
    assert zones[0]["kind"] == "OBL"
    assert zones[0]["end_reason"] == "replaced"
    assert zones[0]["level"] == 10.0


def test_rev3_div_rsi_above_70_still_short() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (80.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (71.0, 22.0, 10.0, 21.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["side"] == "short"
    assert s["ob_level"] == 20.0
    assert s["ob_rsi"] == 80.0
    assert s["sig_rsi"] == 71.0
    assert zones[0]["end_reason"] == "signal"


def test_rev3_div_rsi_below_30_still_long() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (25.0, 12.0, 8.0, 9.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["side"] == "long"
    assert s["ob_level"] == 10.0
    assert s["ob_rsi"] == 20.0
    assert s["sig_rsi"] == 25.0
    assert zones[0]["end_reason"] == "signal"


def test_rev3_old_obh_active_while_new_forming() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (80.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (75.0, 16.0, 10.0, 15.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert zones == []
    rows2 = rows + [(55.0, 22.0, 10.0, 21.0)]
    sigs, zones = _run(rows2)
    assert len(sigs) == 1
    assert sigs[0]["side"] == "short"
    assert sigs[0]["ob_level"] == 20.0
    assert zones[0]["end_reason"] == "signal"


def test_rev3_new_obh_confirm_replaces_old() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (80.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (75.0, 16.0, 10.0, 15.0),
        (55.0, 14.0, 9.0, 10.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert len(zones) == 1
    assert zones[0]["kind"] == "OBH"
    assert zones[0]["level"] == 20.0
    assert zones[0]["end_reason"] == "replaced"


def test_rev3_old_obl_active_while_new_forming() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (25.0, 15.0, 11.0, 12.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert zones == []
    rows2 = rows + [(45.0, 12.0, 8.0, 9.0)]
    sigs, zones = _run(rows2)
    assert len(sigs) == 1
    assert sigs[0]["side"] == "long"
    assert sigs[0]["ob_level"] == 10.0
    assert zones[0]["end_reason"] == "signal"


def test_rev3_new_obl_confirm_replaces_old() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (25.0, 15.0, 11.0, 12.0),
        (40.0, 16.0, 12.0, 13.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert len(zones) == 1
    assert zones[0]["kind"] == "OBL"
    assert zones[0]["level"] == 10.0
    assert zones[0]["end_reason"] == "replaced"


def test_rev4_obh_expires_when_rsi_below_40_before_signal() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (80.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (39.0, 16.0, 10.0, 11.0),
        (50.0, 22.0, 12.0, 21.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert zones[0]["kind"] == "OBH"
    assert zones[0]["end_reason"] == "rsi_below_exp"
    assert zones[0]["end_rsi"] == 39.0


def test_rev4_obl_expires_when_rsi_above_60_before_signal() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (61.0, 16.0, 12.0, 14.0),
        (45.0, 12.0, 8.0, 9.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert zones[0]["kind"] == "OBL"
    assert zones[0]["end_reason"] == "rsi_above_exp"
    assert zones[0]["end_rsi"] == 61.0


def test_rev4_rsi_35_between_30_40_expires_obh() -> None:
    """Rev3 kept OBH at RSI 35; rev4 expires (35 < 40)."""
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (80.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (35.0, 16.0, 10.0, 11.0),
    ]
    sigs, zones = _run(rows)
    assert sigs == []
    assert len(zones) == 1
    assert zones[0]["kind"] == "OBH"
    assert zones[0]["end_reason"] == "rsi_below_exp"
    assert zones[0]["end_rsi"] == 35.0


def test_rev4_rsi_45_obh_stays_then_short() -> None:
    rows = [
        (50.0, 10.0, 9.0, 9.5),
        (80.0, 20.0, 10.0, 19.0),
        (60.0, 18.0, 10.0, 12.0),
        (45.0, 16.0, 10.0, 11.0),
        (50.0, 22.0, 12.0, 21.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    assert sigs[0]["side"] == "short"
    assert sigs[0]["ob_level"] == 20.0
    assert sigs[0]["sig_rsi"] == 50.0
    assert zones[0]["end_reason"] == "signal"


def test_long_rule8_one_signal() -> None:
    rows = [
        (50.0, 20.0, 19.0, 19.5),
        (20.0, 18.0, 10.0, 11.0),
        (40.0, 16.0, 12.0, 13.0),
        (45.0, 12.0, 8.0, 9.0),
        (50.0, 11.0, 7.0, 8.0),
    ]
    sigs, zones = _run(rows)
    assert len(sigs) == 1
    assert zones[0]["end_reason"] == "signal"
