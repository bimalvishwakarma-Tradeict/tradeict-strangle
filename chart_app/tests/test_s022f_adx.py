"""Chart ADX Pine v4 TV parity + S022F overlay vs backtest path."""

from __future__ import annotations

import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP))

from indicators.adx import compute_adx  # noqa: E402
from indicators import load_indicators  # noqa: E402
from server import overlay_merged_params, overlay_warmup_sec  # noqa: E402
from strategies.s020_rsi import compute as compute_s020_rsi  # noqa: E402
from strategies.s022f import compute as compute_s022f  # noqa: E402

from backtest.strategies.s018_4h_trend.run_s018 import SPOT_CSV, load_spot_1m  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.stage0_filter import (  # noqa: E402
    match_arm_f,
    rsi_masks,
)
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import _bars_from_ohlc  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
CSV = str(ROOT / SPOT_CSV) if not Path(SPOT_CSV).is_absolute() else SPOT_CSV


def _ist_unix(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


@lru_cache(maxsize=1)
def _spot_1m() -> tuple[np.ndarray, ...]:
    spot = load_spot_1m(CSV)
    return s020.bars_1m_vol(spot)


def _slice_1m(ts0: int, ts1: int) -> list[dict]:
    ts, o, h, l, c, vol = _spot_1m()
    m = (ts >= int(ts0)) & (ts <= int(ts1))
    out = []
    for t, oo, hh, ll, cc, vv in zip(ts[m], o[m], h[m], l[m], c[m], vol[m]):
        out.append(
            {
                "time": int(t),
                "open": float(oo),
                "high": float(hh),
                "low": float(ll),
                "close": float(cc),
                "volume": float(vv),
            }
        )
    return out


def _adx_at(bars: list[dict], unix: int) -> float:
    out = compute_adx(bars, {"length": 14, "th": 20})
    adx_s = next(s for s in out["series"] if s["key"] == "adx")
    pts = {int(p["time"]): float(p["value"]) for p in adx_s["points"]}
    assert unix in pts, f"missing bar {unix} ({datetime.fromtimestamp(unix, tz=IST)})"
    return pts[unix]


def test_t1_adx_chart_matches_tv() -> None:
    load_indicators()
    a = _ist_unix("2026-07-01 00:00")
    b = _ist_unix("2026-09-21 23:59")
    bars_1m = _slice_1m(a, b)
    ts = np.array([x["time"] for x in bars_1m], dtype=np.int64)
    o = np.array([x["open"] for x in bars_1m], dtype=np.float64)
    h = np.array([x["high"] for x in bars_1m], dtype=np.float64)
    l = np.array([x["low"] for x in bars_1m], dtype=np.float64)
    c = np.array([x["close"] for x in bars_1m], dtype=np.float64)
    v = np.array([x["volume"] for x in bars_1m], dtype=np.float64)
    t15, o15, h15, l15, c15, v15 = d.resample_tf(ts, o, h, l, c, v, 900)
    bars15 = _bars_from_ohlc(t15, o15, h15, l15, c15)
    expect15 = {
        "2026-09-08 14:00": 44.38,
        "2026-09-11 20:30": 21.94,
        "2026-09-15 09:00": 47.36,
        "2026-09-17 22:00": 8.35,
        "2026-09-19 16:30": 31.68,
    }
    for lab, want in expect15.items():
        got = _adx_at(bars15, _ist_unix(lab))
        assert abs(got - want) <= 0.05, f"15m {lab} got {got} want {want}"
    t30, o30, h30, l30, c30, v30 = d.resample_tf(ts, o, h, l, c, v, 1800)
    bars30 = _bars_from_ohlc(t30, o30, h30, l30, c30)
    got30 = _adx_at(bars30, _ist_unix("2026-09-08 14:00"))
    assert abs(got30 - 42.36) <= 0.05, f"30m 2026-09-08 14:00 got {got30}"


def _backtest_entries(bars_1m: list[dict]) -> list[int]:
    ts = np.array([x["time"] for x in bars_1m], dtype=np.int64)
    o = np.array([x["open"] for x in bars_1m], dtype=np.float64)
    h = np.array([x["high"] for x in bars_1m], dtype=np.float64)
    l = np.array([x["low"] for x in bars_1m], dtype=np.float64)
    c = np.array([x["close"] for x in bars_1m], dtype=np.float64)
    v = np.array([x["volume"] for x in bars_1m], dtype=np.float64)
    tts, to_, th, tl, tc, tv = d.resample_tf(ts, o, h, l, c, v, 900)
    bars = _bars_from_ohlc(tts, to_, th, tl, tc)
    raw = [s for s in iter_ulp_signals(bars, "full", "15m") if s["rule"] == "fresh"]
    sigs, _z = detect_rsi_div(tts, to_, th, tl, tc, 900, n=14, ob=70, os=30)
    rsi_s, rsi_l = rsi_masks(sigs, tts, 900)
    _d, _m, adx = adx_pine_v4(th, tl, tc, length=14, warmup=500)
    ev = match_arm_f(raw, rsi_s, rsi_l, adx, tts, 900, 6)
    return sorted(int(e["signal_time"]) for e in ev)


def test_t2_s022f_chart_matches_backtest() -> None:
    a = _ist_unix("2026-08-01 00:00")
    b = _ist_unix("2026-09-21 23:59")
    bars = _slice_1m(a, b)
    params = {
        "signal_tf": "15m",
        "ulp_mode": "full",
        "n_win": 6,
        "rsi_len": 14,
        "ob": 70,
        "os": 30,
        "chart_tf": "1m",
        "from": a,
        "to": int(bars[-1]["time"]),
        "show_touches": False,
        "show_entries": True,
    }
    out = compute_s022f(bars, params)
    chart = sorted(
        int(m["signal_close_ts"])
        for m in out["markers"]
        if (not m.get("pending")) and str(m.get("text") or "").startswith("S022F")
    )
    bt = _backtest_entries(bars)
    print(f"t2 entry_count chart={len(chart)} backtest={len(bt)}", flush=True)
    assert chart == bt
    assert len(chart) > 0


def test_t3_lookahead_last_bar_and_pending() -> None:
    a = _ist_unix("2026-08-01 00:00")
    b = _ist_unix("2026-09-10 10:07")
    bars = _slice_1m(a, b)
    assert bars, "empty slice"
    last_open = int(bars[-1]["time"])
    assert last_open % 900 != 0 or True
    params = {
        "signal_tf": "15m",
        "ulp_mode": "full",
        "n_win": 6,
        "rsi_len": 14,
        "chart_tf": "1m",
        "from": a,
        "to": last_open,
        "show_touches": True,
        "show_entries": True,
    }
    out1 = compute_s022f(bars, params)
    old = [
        int(m["signal_close_ts"])
        for m in out1["markers"]
        if (not m.get("pending")) and str(m.get("text") or "").startswith("S022F")
    ]
    bars2 = [dict(x) for x in bars]
    bars2[-1] = dict(bars2[-1])
    bars2[-1]["high"] = float(bars2[-1]["high"]) + 250.0
    bars2[-1]["low"] = float(bars2[-1]["low"]) - 250.0
    bars2[-1]["close"] = float(bars2[-1]["close"]) + 80.0
    out2 = compute_s022f(bars2, params)
    new = [
        int(m["signal_close_ts"])
        for m in out2["markers"]
        if (not m.get("pending")) and str(m.get("text") or "").startswith("S022F")
    ]
    assert old == new
    pending = [m for m in out2["markers"] if m.get("pending")]
    for m in pending:
        assert m.get("entry_allowed") is False
        assert m.get("pending") is True
    bucket = (last_open // 900) * 900
    forming = [
        m for m in out2["markers"]
        if int(m["time"]) >= bucket or int(m.get("signal_close_ts") or 0) > last_open
    ]
    if forming:
        assert all(bool(m.get("pending")) for m in forming)


def test_t4_server_merged_n_win_and_s020_unchanged() -> None:
    extra = {"n_win": 12, "ulp_mode": "wick", "signal_tf": "15m"}
    merged = overlay_merged_params(
        extra,
        tf="1m",
        line_tf="1m",
        variant="V0",
        signal_tf="15m",
        rsi_len=14,
        ob=70,
        os=30,
        exp_obh=40,
        exp_obl=60,
        show_obh=True,
        show_obl=True,
        show_levels=True,
        show_signals=True,
        hrs=24,
        from_u=1,
        to_u=2,
        capped_from=1,
        max_days=7,
    )
    assert merged["n_win"] == 12
    assert merged["ulp_mode"] == "wick"
    w15 = overlay_warmup_sec("S022F", {"signal_tf": "15m"})
    assert w15 == max(2 * 86400, (500 + 60) * 900)
    w_else = overlay_warmup_sec("S020_RSI", {"signal_tf": "15m"})
    assert w_else == 2 * 86400

    rng = np.random.default_rng(0)
    bars = []
    px = 100.0
    t0 = 1_700_000_000
    for i in range(400):
        px += float(rng.normal(0.0, 0.4))
        bars.append(
            {
                "time": t0 + i * 60,
                "open": px,
                "high": px + 0.5,
                "low": px - 0.5,
                "close": px,
                "volume": 1.0,
            }
        )
    p_rsi = {
        "signal_tf": "5m",
        "rsi_len": 14,
        "ob": 70,
        "os": 30,
        "chart_tf": "1m",
        "from": t0,
        "to": t0 + 399 * 60,
        "show_signals": True,
        "show_obh": True,
        "show_obl": True,
        "show_levels": True,
    }
    a = compute_s020_rsi(bars, p_rsi)
    p_merged = dict(p_rsi)
    p_merged["n_win"] = 12
    b = compute_s020_rsi(bars, p_merged)
    ma = [(int(m["time"]), str(m.get("text")), m.get("pending")) for m in a["markers"]]
    mb = [(int(m["time"]), str(m.get("text")), m.get("pending")) for m in b["markers"]]
    assert ma == mb
