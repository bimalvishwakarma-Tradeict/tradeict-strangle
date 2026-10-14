"""chart_app view + replay e2e (Playwright)."""
from __future__ import annotations

import sys
import time
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

IST = ZoneInfo("Asia/Kolkata")
BASE = "http://127.0.0.1:8700/?v=view1"
REPLAY_0500 = int(datetime(2026, 10, 5, 5, 0, tzinfo=IST).timestamp())
REPLAY_TS = int(datetime(2026, 10, 5, 9, 30, tzinfo=IST).timestamp())
REPLAY_0937 = int(datetime(2026, 10, 5, 9, 37, tzinfo=IST).timestamp())
REPLAY_0945 = int(datetime(2026, 10, 5, 9, 45, tzinfo=IST).timestamp())
CLOSE_0930 = int(datetime(2026, 10, 5, 9, 30, tzinfo=IST).timestamp())
DAY0 = int(datetime(2026, 10, 5, 0, 0, tzinfo=IST).timestamp())
DAY1 = int(datetime(2026, 10, 6, 0, 0, tzinfo=IST).timestamp())
ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "chart_app" / "tests" / "out"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def cmd(page, **detail):
    page.evaluate(
        "(d) => window.dispatchEvent(new CustomEvent('ca-cmd', { detail: d }))",
        detail,
    )


def snap(page):
    cmd(page, cmd="test")
    return page.evaluate("() => window.__caTest")


def wait_ready(page, timeout=45000):
    t0 = time.time()
    last = None
    while (time.time() - t0) * 1000 < timeout:
        last = snap(page)
        if last and last.get("n") and last["n"] > 50 and last.get("lastTime"):
            return last
        page.wait_for_timeout(250)
    raise RuntimeError("chart did not load candles: %s" % last)


def wait_replay(page, timeout=40000, at_ts=None):
    t0 = time.time()
    last = None
    while (time.time() - t0) * 1000 < timeout:
        last = snap(page)
        if last and last.get("replayOn") and last.get("replayBarOpen") and (last.get("n") or 0) > 10:
            if at_ts is None:
                return last
            vr = last.get("vr") or {}
            wide = False
            try:
                wide = (float(vr.get("to")) - float(vr.get("from"))) >= 40 * tf_sec(last)
            except (TypeError, ValueError):
                wide = False
            cur = int(last.get("replayBarOpen") or 0)
            last_t = int(last.get("lastTime") or 0)
            near = abs(cur - int(at_ts)) <= 6 * 60 or abs(last_t - int(at_ts)) <= 6 * 60
            if near and wide:
                return last
        page.wait_for_timeout(200)
    raise RuntimeError("replay did not start: %s" % last)


def http_json(url, timeout=120):
    with urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def no_console_errors(errors):
    bad = []
    for e in errors:
        if e.get("type") != "error":
            continue
        msg = str(e.get("msg") or "")
        if "404" in msg or "favicon" in msg.lower() or "Value is null" in msg:
            continue
        bad.append(e)
    return bad


def tf_sec(s):
    return TF_SEC.get((s or {}).get("tf") or "1m", 60)


def rsi_params_q():
    return quote(json.dumps({
        "signal_tf": "15m",
        "rsi_len": 14,
        "ob": 70,
        "os": 30,
        "exp_obh": 40,
        "exp_obl": 60,
        "show_signals": True,
        "show_obh": True,
        "show_obl": True,
        "show_levels": True,
        "show_rsi_pane": True,
        "hours": 24,
    }))


def confirmed_overlay_sigs(j):
    out = []
    for m in (j or {}).get("markers") or []:
        txt = str(m.get("text") or "")
        if txt not in ("LONG", "SHORT"):
            continue
        if m.get("pending") or m.get("reason") == "pending_bar":
            continue
        close_ts = int(m.get("signal_close_ts") or 0)
        out.append((close_ts, str(m.get("side") or ("long" if txt == "LONG" else "short"))))
    out.sort()
    return out


def fetch_1m_range(origin, start, end):
    rows = []
    t = int(start)
    end = int(end)
    seen = set()
    while t <= end:
        j = http_json("%s/api/candles?tf=1m&start=%s&limit=4000" % (origin, t), timeout=120)
        chunk = j.get("candles") or []
        if not chunk:
            t += 4000 * 60
            if t > end:
                break
            continue
        last = t
        for c in chunk:
            ts = int(c["time"])
            if ts < start or ts > end or ts in seen:
                continue
            seen.add(ts)
            rows.append(c)
            last = ts
        nxt = int(chunk[-1]["time"]) + 60
        if nxt <= t:
            break
        t = nxt
        if last >= end:
            break
    rows.sort(key=lambda x: int(x["time"]))
    return rows


def backtest_rsi_div_day(bars_1m):
    import numpy as np
    from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div
    from backtest.strategies.s020_vwap_touch.run_s020_dev import resample_tf

    ts = np.array([int(b["time"]) for b in bars_1m], dtype=np.int64)
    o = np.array([float(b["open"]) for b in bars_1m], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars_1m], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars_1m], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars_1m], dtype=np.float64)
    v = np.array([float(b.get("volume") or 0.0) for b in bars_1m], dtype=np.float64)
    tts, to_, th, tl, tc, _tv = resample_tf(ts, o, h, l, c, v, 900)
    sigs, _z = detect_rsi_div(tts, to_, th, tl, tc, 900, n=14, ob=70, os=30, exp_obh=40, exp_obl=60)
    day = []
    for s in sigs:
        t = int(s["ts"])
        if DAY0 <= t < DAY1:
            day.append((t, str(s["side"])))
    day.sort()
    return day


def timed_get(url):
    t0 = time.perf_counter()
    j = http_json(url, timeout=120)
    wall = (time.perf_counter() - t0) * 1000.0
    return j, wall


def assert_no_leak(s, label):
    leak = (s or {}).get("leak") or {}
    if leak.get("leak"):
        raise AssertionError("%s future leak %s" % (label, leak))
    last = s.get("lastTime")
    cur = s.get("replayBarOpen")
    if cur is not None and last is not None and last > cur:
        raise AssertionError("%s lastTime %s > cursor %s" % (label, last, cur))


def run(base_url: str) -> list[dict]:
    results = []
    errors = []

    def rec(name, ok, detail=""):
        results.append({"scenario": name, "ok": ok, "detail": detail})
        line = ("PASS" if ok else "FAIL") + " " + name + " " + str(detail)
        print(line.encode("ascii", "replace").decode("ascii"))
        if ok:
            return
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() else "_" for ch in name)[:40]
        try:
            page.screenshot(path=str(OUT_DIR / ("fail_%s.png" % slug)), full_page=True)
        except Exception:
            pass
        slog = OUT_DIR / "server.log"
        if slog.is_file():
            try:
                tail = slog.read_text(encoding="utf-8", errors="replace")[-12000:]
                (OUT_DIR / ("fail_%s_server_tail.txt" % slug)).write_text(tail, encoding="utf-8")
            except OSError:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda exc: errors.append({"type": "error", "msg": str(exc)}))
        page.on("console", lambda msg: errors.append({"type": msg.type, "msg": msg.text}) if msg.type == "error" else None)
        page.goto(base_url, wait_until="domcontentloaded")
        wait_ready(page)

        # 1. LIVE wheel zoom
        try:
            s0 = snap(page)
            box = page.locator("#chart").bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            for _ in range(10):
                page.mouse.wheel(0, -180)
                page.wait_for_timeout(60)
            page.wait_for_timeout(250)
            s1 = snap(page)
            mid0 = s0.get("tCenter") or s0.get("mid")
            mid1 = s1.get("tCenter") or s1.get("mid")
            lim = 2 * tf_sec(s1)
            ok = mid0 is not None and mid1 is not None and abs(mid1 - mid0) <= lim
            rec("1 LIVE wheel zoom 10x center +-2 bars", ok, "mid0=%s mid1=%s lim=%s" % (mid0, mid1, lim))
        except Exception as e:
            rec("1 LIVE wheel zoom 10x center ±2 bars", False, str(e))

        # 2. TF change then wheel
        try:
            jumps = []
            for tfv in ("5m", "15m", "1m"):
                cmd(page, cmd="tf", tf=tfv)
                page.wait_for_timeout(2500)
                s_a = snap(page)
                box = page.locator("#chart").bounding_box()
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.wheel(0, -200)
                page.wait_for_timeout(400)
                s_b = snap(page)
                a = s_a.get("tCenter") or s_a.get("mid")
                b = s_b.get("tCenter") or s_b.get("mid")
                jumps.append(abs((b or 0) - (a or 0)))
            lim = 8 * 60
            ok = all(j <= lim * 8 for j in jumps)  # zoom may shift a bit; no 1500-bar leap
            rec("2 TF 1m-5m-15m-1m + wheel no jump", ok, "jumps=%s" % jumps)
        except Exception as e:
            rec("2 TF 1m-5m-15m-1m + wheel no jump", False, str(e))

        # 3. loadOlder 3x
        try:
            cmd(page, cmd="tf", tf="1m")
            page.wait_for_timeout(1500)
            wait_ready(page, timeout=20000)
            cmd(page, cmd="goRt")
            page.wait_for_timeout(800)
            s0 = snap(page)
            n0 = s0.get("n") or 0
            sec = tf_sec(s0)
            mids = []
            for i in range(3):
                before = snap(page)
                cmd(page, cmd="shift", delta=-80)
                page.wait_for_timeout(900)
                after = snap(page)
                mids.append((before.get("mid"), after.get("mid"), after.get("n"), after.get("tf")))
            s1 = snap(page)
            ok = True
            lim = 80 * sec * 4
            for a, b, n, _tf in mids:
                if a is None or b is None or (n or 0) < 50:
                    continue
                if abs(b - a) > lim:
                    ok = False
                    break
            rec("3 loadOlder / pan no view jump", ok, "n0=%s n1=%s lim=%s mids=%s" % (n0, s1.get("n"), lim, mids))
        except Exception as e:
            rec("3 loadOlder / pan no view jump", False, str(e))

        # 4. Replay 5 Oct 2026 09:30
        try:
            cmd(page, cmd="start", ts=REPLAY_TS)
            s = wait_replay(page)
            assert_no_leak(s, "start")
            for _ in range(20):
                cmd(page, cmd="step")
                page.wait_for_timeout(80)
            s20 = snap(page)
            assert_no_leak(s20, "20step")
            cmd(page, cmd="play")
            page.wait_for_timeout(15000)
            cmd(page, cmd="pause")
            page.wait_for_timeout(400)
            sp = snap(page)
            assert_no_leak(sp, "play")
            cur = sp.get("replayBarOpen")
            ok = cur is not None and cur >= REPLAY_TS
            rec(
                "4 Replay 05 Oct 2026 09:30 step20 play15s",
                ok,
                "cursor=%s last=%s leak=%s" % (cur, sp.get("lastTime"), sp.get("leak")),
            )
        except Exception as e:
            rec("4 Replay 05 Oct 2026 09:30 step20 play15s", False, str(e))

        # 5. Replay TF partial / no leak
        try:
            s0 = snap(page)
            cur0 = s0.get("replayBarOpen")
            last_ok = True
            details = []
            for tfv in ("5m", "15m", "1m"):
                cmd(page, cmd="tf", tf=tfv)
                page.wait_for_timeout(1500)
                s = snap(page)
                assert_no_leak(s, tfv)
                last = s.get("lastTime")
                cur = s.get("replayBarOpen")
                details.append((tfv, last, cur, s.get("lastClose")))
                if cur != cur0:
                    last_ok = False
                if last is not None and last > cur:
                    last_ok = False
                if tfv != "1m" and last is not None:
                    sec = TF_SEC[tfv]
                    if last != (cur // sec) * sec and last > cur:
                        last_ok = False
            rec("5 Replay TF 1m→5m→15m partial no leak", last_ok, "cur0=%s %s" % (cur0, details))
        except Exception as e:
            rec("5 Replay TF 1m→5m→15m partial no leak", False, str(e))

        # 6. Resize + panel
        try:
            cmd(page, cmd="tf", tf="1m")
            page.wait_for_timeout(800)
            s0 = snap(page)
            vr0 = s0.get("vr")
            for w, h in [(1200, 800), (1400, 900), (1100, 700), (1400, 900)] * 2 + [(1400, 900), (1300, 850)]:
                page.set_viewport_size({"width": w, "height": h})
                page.wait_for_timeout(200)
            cmd(page, cmd="panel", pane="paneTrades")
            page.wait_for_timeout(400)
            cmd(page, cmd="panel", pane="paneTrades")
            page.wait_for_timeout(400)
            s1 = snap(page)
            assert_no_leak(s1, "resize")
            vr1 = s1.get("vr")
            ok = vr0 and vr1 and abs(vr0["from"] - vr1["from"]) < 30 * 60 and abs(vr0["to"] - vr1["to"]) < 30 * 60
            rec("6 Replay resize 10x + panel", ok, "vr0=%s vr1=%s" % (vr0, vr1))
        except Exception as e:
            rec("6 Replay resize 10x + panel", False, str(e))

        # 7. Drawings
        try:
            s0 = snap(page)
            vr0 = s0.get("vr")
            cap = s0.get("replayBarOpen") or s0.get("lastTime")
            for item in (
                {"type": "long", "t1": cap - 300, "t2": cap + 50 * 60, "entry": 83100, "target": 83400, "sl": 82900, "p1": 83100, "p2": 83400},
                {"type": "fib", "t1": cap - 180, "t2": cap + 20 * 60, "p1": 84326, "p2": 83567},
                {"type": "rect", "t1": cap - 240, "t2": cap + 10 * 60, "p1": 83000, "p2": 83200},
            ):
                cmd(page, cmd="draw", item=item)
            host = page.locator("#chart")
            box = host.bounding_box()
            for _ in range(5):
                page.mouse.click(box["x"] + 120, box["y"] + 80)
                page.wait_for_timeout(50)
            s1 = snap(page)
            cmd(page, cmd="step")
            page.wait_for_timeout(400)
            s2 = snap(page)
            assert_no_leak(s1, "draw")
            same = s0.get("vr") and s1.get("vr") and abs(s0["vr"]["from"] - s1["vr"]["from"]) < 5 and abs(s0["vr"]["to"] - s1["vr"]["to"]) < 5
            rec("7 Replay drawings click/drag view same", same, "vr0=%s vr1=%s" % (vr0, s1.get("vr")))
        except Exception as e:
            rec("7 Replay drawings click/drag view same", False, str(e))

        # 8. Indicator add/remove
        try:
            s0 = snap(page)
            cmd(page, cmd="addInd", id="ULP")
            page.wait_for_timeout(1500)
            cmd(page, cmd="addStrat", id="S020_RSI")
            page.wait_for_timeout(2000)
            s1 = snap(page)
            assert_no_leak(s1, "ind")
            xa = s1.get("xAlign")
            pane_ok = xa is None or xa.get("tM") is None or xa.get("tR") is None or xa.get("tM") == xa.get("tR")
            same = s0.get("vr") and s1.get("vr") and abs((s0["vr"]["from"] or 0) - (s1["vr"]["from"] or 0)) < 120
            rec("8 Replay ULP/RSI add view same pane sync", same and pane_ok, "xAlign=%s vr0=%s vr1=%s" % (xa, s0.get("vr"), s1.get("vr")))
        except Exception as e:
            rec("8 Replay ULP/RSI add view same pane sync", False, str(e))

        # 10. Server partial HTF candle (EMA 9 + ULP at 09:37 on 15m)
        try:
            cmd(page, cmd="start", ts=REPLAY_0937)
            wait_replay(page)
            cmd(page, cmd="tf", tf="15m")
            page.wait_for_timeout(1800)
            cmd(page, cmd="addInd", id="EMA")
            cmd(page, cmd="addInd", id="ULP")
            page.wait_for_timeout(2800)
            parsed = urlparse(base_url)
            origin = "%s://%s" % (parsed.scheme, parsed.netloc)
            t_open = (REPLAY_0937 // 900) * 900
            j1 = http_json("%s/api/candles?tf=1m&end=%s&limit=30" % (origin, REPLAY_0937), timeout=120)
            m1 = [c for c in (j1.get("candles") or []) if t_open <= int(c["time"]) <= REPLAY_0937]
            if len(m1) < 2:
                raise AssertionError("not enough 1m bars for partial: %s" % len(m1))
            expect = {
                "time": t_open,
                "open": float(m1[0]["open"]),
                "high": max(float(c["high"]) for c in m1),
                "low": min(float(c["low"]) for c in m1),
                "close": float(m1[-1]["close"]),
            }
            from_ts = REPLAY_0937 - 2 * 86400
            pem = quote('{"length":9}')
            je = http_json(
                "%s/api/indicator/EMA?tf=15m&from=%s&to=%s&params=%s" % (origin, from_ts, REPLAY_0937, pem),
                timeout=120,
            )
            lb = je.get("last_bar") or {}
            ohlc_ok = (
                int(lb.get("time") or 0) == expect["time"]
                and abs(float(lb.get("open") or 0) - expect["open"]) < 1e-6
                and abs(float(lb.get("high") or 0) - expect["high"]) < 1e-6
                and abs(float(lb.get("low") or 0) - expect["low"]) < 1e-6
                and abs(float(lb.get("close") or 0) - expect["close"]) < 1e-6
            )
            pts = ((je.get("series") or [{}])[0].get("points") or [])
            last_pt = None
            for p in reversed(pts):
                if int(p.get("time") or 0) == t_open and p.get("value") is not None:
                    last_pt = p
                    break
            ju = http_json(
                "%s/api/indicator/ULP?tf=15m&from=%s&to=%s" % (origin, from_ts, REPLAY_0937),
                timeout=120,
            )
            ulp_lb = ju.get("last_bar") or {}
            ulp_ok = int(ulp_lb.get("time") or 0) == t_open and abs(float(ulp_lb.get("close") or 0) - expect["close"]) < 1e-6
            rov = http_json(
                "%s/api/overlay?strategy=S020&tf=15m&from=%s&to=%s" % (origin, from_ts, REPLAY_0937),
                timeout=120,
            )
            ov = (rov or {}).get("last_bar") or {}
            ov_ok = int(ov.get("time") or 0) == t_open and abs(float(ov.get("close") or 0) - expect["close"]) < 1e-6
            snap_s = snap(page)
            assert_no_leak(snap_s, "s10")
            ok = ohlc_ok and last_pt is not None and ulp_ok and ov_ok
            rec(
                "10 Replay 15m 09:37 EMA+ULP partial last bar",
                ok,
                "expect=%s ema_last=%s ema_pt=%s ulp=%s ov=%s n1m=%s" % (
                    expect, lb, last_pt, ulp_lb, ov, len(m1),
                ),
            )
        except Exception as e:
            rec("10 Replay 15m 09:37 EMA+ULP partial last bar", False, str(e))

        # 11. Closed-bar RSI Div signals + chart==backtest
        try:
            parsed = urlparse(base_url)
            origin = "%s://%s" % (parsed.scheme, parsed.netloc)
            cmd(page, cmd="addStrat", id="S020_RSI")
            page.wait_for_timeout(2500)
            cmd(page, cmd="overlayReload")
            page.wait_for_timeout(2000)
            from_ts = REPLAY_0937 - 2 * 86400
            pq = rsi_params_q()
            ov_url = "%s/api/overlay?strategy=S020_RSI&tf=15m&from=%s&to=%s&params=%s" % (
                origin, from_ts, REPLAY_0937, pq,
            )
            j37, _w1 = timed_get(ov_url)
            j37b, w37_hit = timed_get(ov_url)
            sigs37 = confirmed_overlay_sigs(j37)
            leak37 = [s for s in sigs37 if s[0] > CLOSE_0930]
            s37 = snap(page)
            ui37 = [(int(x["closeTs"]), str(x.get("side") or "")) for x in (s37.get("confirmedSigs") or [])]
            ui_leak = [s for s in ui37 if s[0] > CLOSE_0930]
            n_step = 0
            while n_step < 20:
                cur = snap(page).get("replayBarOpen") or 0
                if cur >= REPLAY_0945:
                    break
                cmd(page, cmd="step")
                page.wait_for_timeout(220)
                n_step += 1
            page.wait_for_timeout(1800)
            cmd(page, cmd="overlayReload")
            page.wait_for_timeout(1500)
            ov45_url = "%s/api/overlay?strategy=S020_RSI&tf=15m&from=%s&to=%s&params=%s" % (
                origin, from_ts, REPLAY_0945, pq,
            )
            j45, _ = timed_get(ov45_url)
            sigs45 = confirmed_overlay_sigs(j45)
            new45 = [s for s in sigs45 if s[0] > CLOSE_0930]
            future45 = [s for s in sigs45 if s[0] > REPLAY_0945]
            s45 = snap(page)
            # full day vs backtest
            end_day = DAY1 - 60
            from_day = DAY0 - 10 * 86400
            ov_day_url = "%s/api/overlay?strategy=S020_RSI&tf=1m&from=%s&to=%s&params=%s" % (
                origin, DAY0 - 3600, end_day, pq,
            )
            jday, _ = timed_get(ov_day_url)
            chart_day = [s for s in confirmed_overlay_sigs(jday) if DAY0 <= s[0] < DAY1]
            bars = fetch_1m_range(origin, from_day, end_day)
            bt_day = backtest_rsi_div_day(bars)
            match = chart_day == bt_day
            rows = []
            keys = sorted(set(chart_day) | set(bt_day))
            for t, side in keys:
                ch = "yes" if (t, side) in set(chart_day) else "no"
                bt = "yes" if (t, side) in set(bt_day) else "no"
                ist = datetime.fromtimestamp(t, IST).strftime("%H:%M")
                rows.append("%s %s chart=%s bt=%s" % (ist, side, ch, bt))
            # timing after exact-to cache: 15m then 1m
            pem = quote('{"length":9}')
            iu15 = "%s/api/indicator/EMA?tf=15m&from=%s&to=%s&params=%s" % (origin, from_ts, REPLAY_0937, pem)
            timed_get(iu15)
            _je, ind15_hit = timed_get(iu15)
            timed_get(ov_url)
            _jo, ov15_hit = timed_get(ov_url)
            cmd(page, cmd="tf", tf="1m")
            page.wait_for_timeout(1800)
            cmd(page, cmd="overlayReload")
            page.wait_for_timeout(1200)
            cmd(page, cmd="overlayReload")
            page.wait_for_timeout(800)
            s1m = snap(page)
            iu1 = "%s/api/indicator/EMA?tf=1m&from=%s&to=%s&params=%s" % (origin, from_ts, REPLAY_0937, pem)
            ov1 = "%s/api/overlay?strategy=S020_RSI&tf=1m&from=%s&to=%s&params=%s" % (
                origin, from_ts, REPLAY_0937, pq,
            )
            timed_get(iu1)
            _j, ind1_hit = timed_get(iu1)
            timed_get(ov1)
            _j, ov1_hit = timed_get(ov1)
            ok = (
                len(leak37) == 0
                and len(ui_leak) == 0
                and len(future45) == 0
                and match
            )
            rec(
                "11 RSI Div closed-bar + chart==backtest",
                ok,
                "leak37=%s new45=%s future45=%s match=%s n_chart=%s n_bt=%s table=%s ov15_hit_ms=%.1f ind15_hit_ms=%.1f ov1_hit_ms=%.1f ind1_hit_ms=%.1f ui_ov=%s ui_ind=%s steps=%s" % (
                    leak37, new45, future45, match, len(chart_day), len(bt_day),
                    rows, ov15_hit, ind15_hit, ov1_hit, ind1_hit,
                    s1m.get("overlayMs"), s1m.get("indMs"), n_step,
                ),
            )
        except Exception as e:
            rec("11 RSI Div closed-bar + chart==backtest", False, str(e))

        # 12. Replay step view stability
        try:
            def Number_or(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return 0.0

            def vr_pair(s):
                vr = s.get("vr") or {}
                return (vr.get("from"), vr.get("to"), s.get("barSpacing"))

            def run_follow_off_on(tfv):
                cmd(page, cmd="tf", tf="1m")
                t_1m = time.time()
                while time.time() - t_1m < 15:
                    st = snap(page)
                    if st.get("tf") == "1m":
                        break
                    page.wait_for_timeout(200)
                cmd(page, cmd="start", ts=REPLAY_0500)
                try:
                    wait_replay(page, at_ts=REPLAY_0500)
                except RuntimeError:
                    page.wait_for_timeout(2000)
                    cmd(page, cmd="start", ts=REPLAY_0500)
                    wait_replay(page, at_ts=REPLAY_0500)
                page.wait_for_timeout(800)
                s_1m = snap(page)
                bs_1m = s_1m.get("barSpacing")
                vr_1m = s_1m.get("vr") or {}
                try:
                    w_1m = float(vr_1m.get("to")) - float(vr_1m.get("from"))
                    vis_bars = max(8.0, w_1m / 60.0)
                except (TypeError, ValueError):
                    vis_bars = 80.0
                spacing_ok = True
                if tfv != "1m":
                    cmd(page, cmd="tf", tf=tfv)
                    t_tf = time.time()
                    sec_tf = TF_SEC.get(tfv, 60)
                    want_w = vis_bars * sec_tf
                    while time.time() - t_tf < 25:
                        st = snap(page)
                        last = st.get("lastTime")
                        bucket = st.get("lastTfBucket")
                        spaced = True
                        if bs_1m is not None and st.get("barSpacing") is not None:
                            spaced = abs(Number_or(st.get("barSpacing")) - Number_or(bs_1m)) <= 1.0
                        laid = False
                        try:
                            w = float((st.get("vr") or {}).get("to")) - float((st.get("vr") or {}).get("from"))
                            laid = abs(w - want_w) <= max(8 * sec_tf, 0.2 * want_w)
                        except (TypeError, ValueError):
                            laid = False
                        aligned = (
                            last is not None
                            and bucket is not None
                            and int(last) == int(bucket)
                            and int(last) % sec_tf == 0
                        )
                        busy = bool(st.get("tfBusy"))
                        if st.get("tf") == tfv and (st.get("n") or 0) > 10 and spaced and laid and aligned and not busy:
                            break
                        page.wait_for_timeout(250)
                    page.wait_for_timeout(400)
                    st_tf = snap(page)
                    if bs_1m is not None and st_tf.get("barSpacing") is not None:
                        if abs(Number_or(st_tf.get("barSpacing")) - Number_or(bs_1m)) > 1.0:
                            spacing_ok = False
                    if st_tf.get("tf") != tfv:
                        spacing_ok = False
                cmd(page, cmd="follow")
                page.wait_for_timeout(400)
                cmd(page, cmd="followOff")
                page.wait_for_timeout(300)
                s0 = snap(page)
                a0 = vr_pair(s0)
                off_ok = True
                for _ in range(20):
                    cmd(page, cmd="step")
                    page.wait_for_timeout(140)
                page.wait_for_timeout(400)
                s1 = snap(page)
                a1 = vr_pair(s1)
                sec_off = TF_SEC.get(tfv, 60)
                if abs(Number_or(a0[0]) - Number_or(a1[0])) > 2 * sec_off:
                    off_ok = False
                if a0[2] is not None and a1[2] is not None and abs(Number_or(a0[2]) - Number_or(a1[2])) > 0.5:
                    off_ok = False
                cmd(page, cmd="follow")
                page.wait_for_timeout(400)
                prev = snap(page)
                pvr = prev.get("vr") or {}
                max_shift = 0.0
                hold_fail = False
                lock_fail = False
                sec = TF_SEC.get(tfv, 60)
                bs0 = prev.get("barSpacing")
                bs_ok = True
                bucket_ok = True
                last_vis = prev.get("lastTime")
                bucket = prev.get("lastTfBucket")
                if last_vis is None or bucket is None or int(last_vis) != int(bucket):
                    bucket_ok = False
                for _ in range(40):
                    last_before = prev.get("lastTime")
                    gap_before = None
                    if last_before is not None and pvr.get("to") is not None:
                        gap_before = (Number_or(pvr.get("to")) - Number_or(last_before)) / sec
                    cmd(page, cmd="step")
                    page.wait_for_timeout(140)
                    cur = snap(page)
                    cvr = cur.get("vr") or {}
                    last = cur.get("lastTime")
                    dfrom = abs(Number_or(cvr.get("from")) - Number_or(pvr.get("from")))
                    dto = abs(Number_or(cvr.get("to")) - Number_or(pvr.get("to")))
                    max_shift = max(max_shift, dfrom / sec, dto / sec)
                    if gap_before is not None and gap_before >= 10.5:
                        if dfrom / sec > 0.35 or dto / sec > 0.35:
                            hold_fail = True
                    if gap_before is not None and gap_before < 9.5:
                        if last is not None and cvr.get("to") is not None:
                            gap_after = (Number_or(cvr.get("to")) - Number_or(last)) / sec
                            if abs(gap_after - gap_before) > 1.01:
                                lock_fail = True
                    if bs0 is not None and cur.get("barSpacing") is not None:
                        if abs(Number_or(cur.get("barSpacing")) - Number_or(bs0)) > 0.5:
                            bs_ok = False
                    prev = cur
                    pvr = cvr
                    if cur.get("lastTime") is not None and cur.get("lastTfBucket") is not None:
                        if int(cur.get("lastTime")) != int(cur.get("lastTfBucket")):
                            bucket_ok = False
                on_ok = (
                    max_shift <= 1.01
                    and (not hold_fail)
                    and (not lock_fail)
                    and spacing_ok
                    and bucket_ok
                )
                return off_ok, on_ok, a0, a1, max_shift, hold_fail, lock_fail, bs_ok, spacing_ok, bucket_ok

            r1 = run_follow_off_on("1m")
            r5 = run_follow_off_on("5m")
            r15 = run_follow_off_on("15m")
            ok = all(r[0] and r[1] for r in (r1, r5, r15))
            rec(
                "12 Replay step view Follow OFF/ON 1m-5m-15m",
                ok,
                "1m off=%s on=%s sp/bk=%s/%s a0=%s a1=%s shift=%.3f hold/lock=%s/%s 5m off=%s on=%s sp/bk=%s/%s a0=%s a1=%s 15m off=%s on=%s sp/bk=%s/%s a0=%s a1=%s" % (
                    r1[0], r1[1], r1[8], r1[9], r1[2], r1[3], r1[4], r1[5], r1[6],
                    r5[0], r5[1], r5[8], r5[9], r5[2], r5[3],
                    r15[0], r15[1], r15[8], r15[9], r15[2], r15[3],
                ),
            )
        except Exception as e:
            rec("12 Replay step view Follow OFF/ON 1m-5m-15m", False, str(e))

        # 13. VWAP 05:30 IST session reset break
        try:
            parsed = urlparse(base_url)
            origin = "%s://%s" % (parsed.scheme, parsed.netloc)
            windows = []
            for day in (4, 5):
                t0 = int(datetime(2026, 10, day, 5, 27, tzinfo=IST).timestamp())
                t1 = int(datetime(2026, 10, day, 5, 33, tzinfo=IST).timestamp())
                windows.append((day, t0, t1))
            from_ts = int(datetime(2026, 10, 4, 0, 0, tzinfo=IST).timestamp()) - 3600
            to_ts = int(datetime(2026, 10, 5, 6, 0, tzinfo=IST).timestamp())
            pq = quote(json.dumps({"line_tf": "1m", "hours": 48, "show_vwap": True}))
            jov = http_json(
                "%s/api/overlay?strategy=S020&tf=1m&from=%s&to=%s&params=%s" % (origin, from_ts, to_ts, pq),
                timeout=120,
            )
            vline = None
            for ln in (jov.get("lines") or []):
                if ln.get("kind") == "vwap":
                    vline = ln
                    break
            pts = (vline or {}).get("points") or []
            table = []
            ok13 = True
            import numpy as np
            from backtest.strategies.s020_vwap_touch.run_s020 import session_vwap as bt_vwap

            bars = fetch_1m_range(origin, from_ts, to_ts)
            ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
            h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
            l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
            c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
            v = np.array([float(b.get("volume") or 0.0) for b in bars], dtype=np.float64)
            bt = bt_vwap(ts, h, l, c, v)
            bt_map = {int(ts[i]): float(bt[i]) if np.isfinite(bt[i]) else None for i in range(len(ts))}
            pt_map = {}
            breaks = []
            prev_val = None
            prev_t = None
            for p in pts:
                t = int(p.get("time") or 0)
                if "value" not in p or p.get("value") is None:
                    breaks.append(t)
                    prev_val = None
                    prev_t = t
                    continue
                val = float(p["value"])
                pt_map[t] = val
                if prev_val is not None and prev_t is not None and abs(val - prev_val) > abs(prev_val) * 0.01:
                    has_break = any(prev_t < b <= t for b in breaks)
                    if not has_break:
                        ok13 = False
                prev_val = val
                prev_t = t
            for day, t0, t1 in windows:
                for t in range(t0, t1 + 1, 60):
                    ist = datetime.fromtimestamp(t, IST).strftime("%d %H:%M")
                    cv = pt_map.get(t)
                    bv = bt_map.get(t)
                    table.append("%s chart=%s bt=%s" % (ist, cv, bv))
                    if cv is not None and (not np.isfinite(cv) or abs(cv) < 1 or abs(cv) > 1e7):
                        ok13 = False
            reset_t = int(datetime(2026, 10, 5, 5, 30, tzinfo=IST).timestamp())
            has_gap = any(abs(b - reset_t) <= 60 for b in breaks)
            rec(
                "13 VWAP 05:30 reset break",
                ok13 and has_gap,
                "gap=%s breaks=%s table=%s" % (has_gap, breaks[:8], table),
            )
        except Exception as e:
            rec("13 VWAP 05:30 reset break", False, str(e))

        # 14. Replay-synced indicators + RSI pane no leak
        try:
            for ind_id in ("EMA", "Supertrend", "ULP"):
                cmd(page, cmd="addInd", id=ind_id)
                page.wait_for_timeout(400)
            cmd(page, cmd="addStrat", id="S020_RSI")
            page.wait_for_timeout(600)
            cmd(page, cmd="tf", tf="1m")
            page.wait_for_timeout(400)
            cmd(page, cmd="start", ts=REPLAY_0500)
            wait_replay(page, at_ts=REPLAY_0500)
            page.wait_for_timeout(1500)
            cmd(page, cmd="overlayReload")
            page.wait_for_timeout(1200)
            s0 = snap(page)
            cap0 = int(s0.get("replayBarOpen") or 0)
            sm0 = s0.get("seriesMax") or {}
            items0 = sm0.get("items") or {}
            ok14a = True
            for _n, mx in items0.items():
                if mx is not None and cap0 and int(mx) > cap0:
                    ok14a = False
            if sm0.get("max") is not None and cap0 and int(sm0["max"]) > cap0:
                ok14a = False
            for _ in range(30):
                cmd(page, cmd="step")
                page.wait_for_timeout(120)
            page.wait_for_timeout(900)
            s1 = snap(page)
            cap1 = int(s1.get("replayBarOpen") or 0)
            sm1 = s1.get("seriesMax") or {}
            items1 = sm1.get("items") or {}
            ok14b = cap1 == REPLAY_0500 + 30 * 60
            for _n, mx in items1.items():
                if mx is None:
                    continue
                if int(mx) > cap1:
                    ok14b = False
            line_ok = False
            for key, mx in items1.items():
                if mx is None:
                    continue
                kl = str(key).lower()
                if "ema" in kl or key == "candles" or "rsi" in kl or "supertrend" in kl or kl.startswith("st"):
                    if int(mx) == cap1:
                        line_ok = True
                    elif int(mx) != cap1 and ("ema" in kl or key == "candles" or "rsi" in kl):
                        ok14b = False
            if items1.get("candles") is not None and int(items1.get("candles")) != cap1:
                ok14b = False
            cmd(page, cmd="tf", tf="5m")
            t5 = time.time()
            while time.time() - t5 < 15:
                st = snap(page)
                if st.get("tf") == "5m" and (st.get("n") or 0) > 10:
                    break
                page.wait_for_timeout(250)
            page.wait_for_timeout(800)
            for _ in range(10):
                cmd(page, cmd="step")
                page.wait_for_timeout(140)
            page.wait_for_timeout(700)
            s5 = snap(page)
            cap5 = int(s5.get("replayBarOpen") or 0)
            sm5 = s5.get("seriesMax") or {}
            ok14c = True
            for _n, mx in (sm5.get("items") or {}).items():
                if mx is not None and int(mx) > cap5:
                    ok14c = False
            ok14 = ok14a and ok14b and line_ok and ok14c
            rec(
                "14 Replay series follow cursor no leak",
                ok14,
                "start_ok=%s step_ok=%s line_ok=%s cap1=%s items1=%s 5m_ok=%s cap5=%s items5=%s" % (
                    ok14a, ok14b, line_ok, cap1, items1, ok14c, cap5, sm5.get("items"),
                ),
            )
        except Exception as e:
            rec("14 Replay series follow cursor no leak", False, str(e))

        # 9. Jump to real-time
        try:
            cmd(page, cmd="live")
            page.wait_for_timeout(2500)
            s = snap(page)
            ok = (not s.get("replayOn")) and s.get("mode") == "LIVE"
            rec("9 Jump to real-time LIVE + WS", ok, "mode=%s ws=%s replay=%s" % (s.get("mode"), s.get("ws"), s.get("replayOn")))
        except Exception as e:
            rec("9 Jump to real-time LIVE + WS", False, str(e))

        # 15. Live mock ticks + new candle + countdown
        try:
            parsed = urlparse(base_url)
            origin = "%s://%s" % (parsed.scheme, parsed.netloc)
            cmd(page, cmd="tf", tf="1m")
            page.wait_for_timeout(800)
            wait_ready(page, timeout=20000)
            page.wait_for_timeout(800)
            s0 = snap(page)
            last_t = int(s0.get("lastTime") or 0)
            last_c = float(s0.get("lastClose") or 100000)
            n0 = int(s0.get("n") or 0)
            nxt = last_t + 60
            prices = [last_c + 5, last_c + 12, last_c + 20]
            def post_tick(px, ts):
                page.evaluate(
                    """([url, price, ts]) => fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({price, ts})
                    })""",
                    ["%s/api/test/tick" % origin, px, ts],
                )
            t_ws = time.time()
            while time.time() - t_ws < 8:
                if snap(page).get("ws"):
                    break
                page.wait_for_timeout(250)
            for px in prices:
                tcur = int((snap(page).get("lastTime") or last_t))
                post_tick(px, tcur + 5)
                page.evaluate(
                    "([p, t]) => window.__caTick && window.__caTick(p, t, {channel:'mock', latency_ms:0})",
                    [float(px), int(tcur + 5)],
                )
                page.wait_for_timeout(200)
            s1 = snap(page)
            close_ok = abs(float(s1.get("lastClose") or 0) - prices[-1]) < 0.01
            post_tick(prices[-1] + 8, nxt + 5)
            page.wait_for_timeout(400)
            s2 = snap(page)
            cmd(page, cmd="injectTick", price=prices[-1] + 8, ts=int(nxt + 5))
            page.wait_for_timeout(300)
            s2 = snap(page)
            new_ok = int(s2.get("lastTime") or 0) >= nxt and int(s2.get("n") or 0) >= n0
            cmd(page, cmd="now", ts=nxt + 10)
            page.wait_for_timeout(200)
            c0 = str((snap(page).get("countdown") or "00:00"))
            cmd(page, cmd="now", ts=nxt + 25)
            page.wait_for_timeout(200)
            c1 = str((snap(page).get("countdown") or "00:00"))
            def cd_sec(txt):
                parts = str(txt).split(":")
                if len(parts) != 2:
                    return None
                return int(parts[0]) * 60 + int(parts[1])
            a = cd_sec(c0)
            b = cd_sec(c1)
            cd_ok = a is not None and b is not None and b < a
            st = http_json("%s/api/ws/status" % origin, timeout=10)
            rec(
                "15 Live mock ticks + countdown",
                close_ok and new_ok and cd_ok,
                "close_ok=%s new_ok=%s cd=%s->%s ws=%s ch=%s lat=%s n0=%s n1=%s last0=%s last2=%s lc0=%s lc1=%s want=%s" % (
                    close_ok, new_ok, c0, c1, s2.get("ws"),
                    s2.get("tickChannel") or st.get("channel"),
                    s2.get("tickLatency") if s2.get("tickLatency") is not None else st.get("latency_ms"),
                    n0, s2.get("n"), last_t, s2.get("lastTime"),
                    last_c, s1.get("lastClose"), prices[-1],
                ),
            )
        except Exception as e:
            rec("15 Live mock ticks + countdown", False, str(e))

        # 16. VWAP session-start warmup vs backtest
        try:
            parsed = urlparse(base_url)
            origin = "%s://%s" % (parsed.scheme, parsed.netloc)
            import numpy as np
            from backtest.strategies.s020_vwap_touch.run_s020 import session_vwap as bt_vwap
            rng = [3, 4, 5]
            samples = []
            for day in rng:
                for hh, mm in ((6, 11), (8, 44), (11, 3), (13, 27), (15, 50), (17, 8), (18, 41), (20, 16), (21, 55), (23, 2)):
                    samples.append(int(datetime(2026, 10, day, hh, mm, tzinfo=IST).timestamp()))
            ok16 = True
            rows16 = []
            by_day = {}
            for t in samples:
                sess0 = (int(t) // 86400) * 86400
                by_day.setdefault(sess0, []).append(t)
            for sess0, times in by_day.items():
                to_t = max(times)
                pq = quote(json.dumps({"line_tf": "1m", "hours": 24, "show_vwap": True}))
                jov = http_json(
                    "%s/api/overlay?strategy=S020&tf=1m&from=%s&to=%s&params=%s" % (origin, sess0, to_t, pq),
                    timeout=120,
                )
                vline = None
                for ln in (jov.get("lines") or []):
                    if ln.get("kind") == "vwap":
                        vline = ln
                        break
                pt_map = {}
                for p in (vline or {}).get("points") or []:
                    if p.get("value") is None:
                        continue
                    pt_map[int(p["time"])] = float(p["value"])
                bars = fetch_1m_range(origin, sess0, to_t)
                ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
                h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
                l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
                c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
                v = np.array([float(b.get("volume") or 0.0) for b in bars], dtype=np.float64)
                bt = bt_vwap(ts, h, l, c, v)
                bt_map = {int(ts[i]): float(bt[i]) for i in range(len(ts)) if np.isfinite(bt[i])}
                for t in times:
                    cv = pt_map.get(t)
                    bv = bt_map.get(t)
                    ist = datetime.fromtimestamp(t, IST).strftime("%d %H:%M")
                    rows16.append("%s chart=%s bt=%s" % (ist, cv, bv))
                    if cv is None or bv is None or abs(float(cv) - float(bv)) > 0.01:
                        ok16 = False
            rec("16 VWAP session warmup vs backtest", ok16, "n=%s rows=%s" % (len(rows16), rows16[:6]))
        except Exception as e:
            rec("16 VWAP session warmup vs backtest", False, str(e))

        def wait_tf(page, tfv, timeout=25):
            t0 = time.time()
            last = None
            need = 40 if tfv in ("1h", "4h") else 100
            while time.time() - t0 < timeout:
                last = snap(page)
                if last.get("tf") == tfv and (last.get("n") or 0) > need and not last.get("tfBusy"):
                    return last
                page.wait_for_timeout(150)
            return last

        # 17. LIVE drag stays, >> then jump-live
        try:
            parsed = urlparse(base_url)
            origin = "%s://%s" % (parsed.scheme, parsed.netloc)
            cmd(page, cmd="live")
            page.wait_for_timeout(1500)
            cmd(page, cmd="tf", tf="1m")
            page.wait_for_timeout(800)
            wait_ready(page, timeout=20000)
            cmd(page, cmd="shift", delta=-50)
            page.wait_for_timeout(500)
            s0 = snap(page)
            vr0 = dict(s0.get("vr") or {})
            px = float(s0.get("lastClose") or 100000)
            tcur = int(s0.get("lastTime") or 0)
            t_end = time.time() + 10
            i = 0
            while time.time() < t_end:
                i += 1
                page.evaluate(
                    "([p, t]) => window.__caTick && window.__caTick(p, t, {channel:'mock', latency_ms:0})",
                    [px + i, tcur + 5],
                )
                page.wait_for_timeout(900)
            s1 = snap(page)
            vr1 = dict(s1.get("vr") or {})
            same = abs(float(vr0.get("from") or 0) - float(vr1.get("from") or 0)) <= 5
            same = same and abs(float(vr0.get("to") or 0) - float(vr1.get("to") or 0)) <= 5
            go = bool(s1.get("goRt") or s1.get("historyMode"))
            cmd(page, cmd="goRt")
            page.wait_for_timeout(700)
            s2 = snap(page)
            last = float(s2.get("lastTime") or 0)
            to = float((s2.get("vr") or {}).get("to") or 0)
            sec = 60.0
            offset_bars = (to - last) / sec if last else 0
            back = (not s2.get("historyMode")) and last and to >= last - 2 * sec
            pad_ok = 3 <= offset_bars <= 12 or back
            rec(
                "17 LIVE drag 50 bars history + goRt",
                same and go and pad_ok,
                "same=%s goRt=%s hist=%s off=%.2f back=%s vr0=%s vr1=%s" % (
                    same, s1.get("goRt"), s1.get("historyMode"), offset_bars, back, vr0, vr1,
                ),
            )
        except Exception as e:
            rec("17 LIVE drag 50 bars history + goRt", False, str(e))

        # 18. TF cycle LIVE then replay
        try:
            cmd(page, cmd="live")
            page.wait_for_timeout(1200)
            for ind_id in ("ULP", "Supertrend"):
                cmd(page, cmd="addInd", id=ind_id)
            cycle = ["1m", "5m", "1h", "4h", "15m", "1m", "4h", "1m"]
            rows18 = []
            ok18 = True
            stale0 = int(snap(page).get("stale") or 0)
            for tfv in cycle:
                t0 = time.time()
                cmd(page, cmd="tf", tf=tfv)
                st = wait_tf(page, tfv, timeout=20)
                dt = (time.time() - t0) * 1000.0
                client_ms = float(st.get("tfMs") or dt)
                legend = str(st.get("legendTf") or "")
                n = int(st.get("n") or 0)
                need = 40 if tfv in ("1h", "4h") else 100
                step_ok = legend == tfv and n > need and client_ms < 2000
                if not step_ok:
                    ok18 = False
                rows18.append("%s n=%s ms=%.0f legend=%s" % (tfv, n, client_ms, legend))
            stale1 = int(snap(page).get("stale") or 0)
            if stale1 > stale0:
                ok18 = False
            cmd(page, cmd="live")
            page.wait_for_timeout(600)
            cmd(page, cmd="tf", tf="1m")
            wait_tf(page, "1m", timeout=20)
            cmd(page, cmd="start", ts=REPLAY_0500)
            wait_replay(page, at_ts=REPLAY_0500)
            for tfv in ["1m", "5m", "15m", "1h"]:
                cmd(page, cmd="tf", tf=tfv)
                st = wait_tf(page, tfv, timeout=20)
                sm = st.get("seriesMax") or {}
                cap = int(st.get("replayBarOpen") or 0)
                leak = False
                for _n, mx in (sm.get("items") or {}).items():
                    if mx is not None and cap and int(mx) > cap:
                        leak = True
                if leak or (st.get("n") or 0) < 10:
                    ok18 = False
                rows18.append("replay %s n=%s leak=%s" % (tfv, st.get("n"), leak))
            rec("18 TF cycle LIVE+replay native HTF", ok18, "stale=%s/%s %s" % (stale0, stale1, rows18))
        except Exception as e:
            rec("18 TF cycle LIVE+replay native HTF", False, str(e))

        cons = no_console_errors(errors)
        rec("console no errors", len(cons) == 0, str(cons[:5]))
        browser.close()
    return results


def run_live_smoke(base_url: str) -> list[dict]:
    results = []
    errors = []

    def rec(name, ok, detail=""):
        results.append({"scenario": name, "ok": ok, "detail": detail})
        line = ("PASS" if ok else "FAIL") + " " + name + " " + str(detail)
        print(line.encode("ascii", "replace").decode("ascii"))
        if ok:
            return
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() else "_" for ch in name)[:40]
        try:
            page.screenshot(path=str(OUT_DIR / ("fail_%s.png" % slug)), full_page=True)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda exc: errors.append({"type": "error", "msg": str(exc)}))
        page.goto(base_url, wait_until="domcontentloaded")
        parsed = urlparse(base_url)
        origin = "%s://%s" % (parsed.scheme, parsed.netloc)
        try:
            s = wait_ready(page, timeout=60000)
            rec("live smoke candles load", (s.get("n") or 0) > 50, "n=%s last=%s" % (s.get("n"), s.get("lastTime")))
        except Exception as e:
            rec("live smoke candles load", False, str(e))
            browser.close()
            return results
        try:
            cmd(page, cmd="live")
            page.wait_for_timeout(1000)
            c0 = str(snap(page).get("countdown") or "")
            t0 = time.time()
            n0 = int(snap(page).get("tickCount") or 0)
            while time.time() - t0 < 60:
                page.wait_for_timeout(2000)
            s = snap(page)
            st = http_json("%s/api/ws/status" % origin, timeout=10)
            n1 = int(s.get("tickCount") or 0)
            ticks = max(n1 - n0, int(st.get("tick_count") or 0))
            c1 = str(s.get("countdown") or "")
            ok_l = ticks >= 10 and c1 != ""
            rec(
                "live 60s real feed",
                ok_l,
                "ticks=%s cd=%s->%s status=%s" % (ticks, c0, c1, st),
            )
        except Exception as e:
            rec("live 60s real feed", False, str(e))
        browser.close()
    return results


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else BASE
    rows = run_live_smoke(url) if "--live" in sys.argv else run(url)
    failed = [r for r in rows if not r["ok"]]
    print("\n--- summary ---")
    for r in rows:
        line = "%s  %s  %s" % ("PASS" if r["ok"] else "FAIL", r["scenario"], r["detail"])
        print(line.encode("ascii", "replace").decode("ascii"))
    sys.exit(1 if failed else 0)
