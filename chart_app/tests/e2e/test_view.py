"""chart_app view + replay e2e (Playwright)."""
from __future__ import annotations

import sys
import time
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
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


def last_frac(s):
    lr = (s or {}).get("lr") or {}
    last_i = (s or {}).get("lastIdx")
    try:
        a = float(lr.get("from"))
        b = float(lr.get("to"))
        i = float(last_i)
    except (TypeError, ValueError):
        return None
    w = b - a
    if w <= 1:
        return None
    return (i - a) / w


def http_post_json(url, body, timeout=30):
    req = Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ws_status_ms(origin: str) -> float:
    t0 = time.time()
    http_json("%s/api/ws/status" % origin, timeout=2)
    return (time.time() - t0) * 1000.0


def overlay_fetch(origin, tf, to_ts, from_ts=None, timeout=120):
    if from_ts is None:
        from_ts = int(to_ts) - 3 * 86400
    params = quote(json.dumps({"max_days": 7, "hours": 72, "show_vwap": True, "line_tf": "1m"}))
    url = "%s/api/overlay?strategy=S020&tf=%s&from=%s&to=%s&params=%s" % (
        origin, tf, int(from_ts), int(to_ts), params,
    )
    t0 = time.time()
    with urlopen(url, timeout=timeout) as r:
        raw = r.read()
    sec = time.time() - t0
    payload = json.loads(raw.decode("utf-8"))
    return {
        "sec": sec,
        "mb": len(raw) / (1024.0 * 1024.0),
        "server_ms": payload.get("server_ms"),
        "capped_from": payload.get("capped_from"),
        "bars_used": payload.get("bars_used"),
        "n_lines": len(payload.get("lines") or []),
        "n_marks": len(payload.get("markers") or []),
        "payload": payload,
        "bytes": len(raw),
    }


def run_overlay_perf_tests(page, rec) -> None:
    parsed = urlparse(page.url)
    origin = "%s://%s" % (parsed.scheme, parsed.netloc)

    def wait_tf_local(tfv, timeout=25):
        t0 = time.time()
        last = None
        need = 40 if tfv in ("1h", "4h") else 100
        while time.time() - t0 < timeout:
            last = snap(page)
            if last.get("tf") == tfv and (last.get("n") or 0) > need and not last.get("tfBusy"):
                return last
            page.wait_for_timeout(150)
        return last

    try:
        cmd(page, cmd="live")
        page.wait_for_timeout(800)
        cmd(page, cmd="tf", tf="1m")
        wait_tf_local("1m", timeout=20)
        cmd(page, cmd="addStrat", id="S020")
        cmd(page, cmd="addInd", id="ULP")
        cmd(page, cmd="addInd", id="ST")
        page.wait_for_timeout(400)
        cmd(page, cmd="overlayReload")
        t_ov = time.time()
        while time.time() - t_ov < 20:
            st = snap(page)
            if not st.get("overlayBusy"):
                break
            ws_ms = min(_ws_status_ms(origin) for _ in range(2))
            if ws_ms >= 2000:
                rec("19 LIVE S020+ULP+ST TF overlay", False, "ws/status blocked ms=%.0f" % ws_ms)
                raise RuntimeError("blocked")
            page.wait_for_timeout(150)
        page.wait_for_timeout(12000)
        rows19 = []
        ok19 = True
        blocked = False
        for tfv in ("1m", "15m", "1h", "4h", "1m"):
            t0 = time.time()
            cmd(page, cmd="tf", tf=tfv)
            saw_busy = False
            while time.time() - t0 < 20:
                st = snap(page)
                if st.get("overlayBusy"):
                    saw_busy = True
                    ws_ms = min(_ws_status_ms(origin) for _ in range(2))
                    if ws_ms >= 2000:
                        ok19 = False
                        blocked = True
                        rows19.append("%s ws_block=%.0f" % (tfv, ws_ms))
                        break
                candle_ready = st.get("tf") == tfv and (st.get("n") or 0) > 40 and not st.get("tfBusy")
                ov_ready = not st.get("overlayBusy")
                if candle_ready and ov_ready:
                    break
                page.wait_for_timeout(120)
            dt = time.time() - t0
            st = snap(page)
            ov_ms = float(st.get("overlayMs") or dt * 1000.0)
            candle_ms = float(st.get("tfMs") or dt * 1000.0)
            step_ok = candle_ms < 2000 and ov_ms < 3000 and st.get("tf") == tfv
            if not step_ok:
                ok19 = False
            rows19.append(
                "%s candle_ms=%.0f ov_ms=%.0f n=%s busy_seen=%s" % (
                    tfv, candle_ms, ov_ms, st.get("n"), saw_busy,
                )
            )
        rec("19 LIVE S020+ULP+ST TF overlay", ok19 and not blocked, str(rows19))
    except RuntimeError:
        pass
    except Exception as e:
        rec("19 LIVE S020+ULP+ST TF overlay", False, str(e))

    try:
        cmd(page, cmd="live")
        page.wait_for_timeout(800)
        cmd(page, cmd="tf", tf="1m")
        wait_tf_local("1m", timeout=20)
        cmd(page, cmd="lastMid")
        page.wait_for_timeout(500)
        s0 = snap(page)
        x0 = s0.get("lastX")
        f0 = last_frac(s0)
        last0 = int(s0.get("lastTime") or 0)
        px = float(s0.get("lastClose") or 100000)
        bs = float(s0.get("barSpacing") or 14)
        lr0 = dict(s0.get("lr") or {})
        wpx = float(page.evaluate("() => (document.getElementById('chart')||{}).clientWidth || 1400"))
        mid_ok = (f0 is not None and 0.22 <= f0 <= 0.78) or (
            x0 is not None and (0.22 * wpx) <= float(x0) <= (0.78 * wpx)
        )
        for i in (1, 2):
            ts = last0 + i * 60
            http_post_json("%s/api/test/tick" % origin, {"price": px + i, "ts": ts})
            t_wait = time.time()
            while time.time() - t_wait < 4:
                cur = snap(page)
                if int(cur.get("lastTime") or 0) >= ts:
                    break
                page.wait_for_timeout(120)
            page.wait_for_timeout(200)
        s1 = snap(page)
        x1 = s1.get("lastX")
        f1 = last_frac(s1)
        lr1 = dict(s1.get("lr") or {})
        dx = abs(float(x1 or 0) - float(x0 or 0)) if x0 is not None and x1 is not None else 999
        df = abs(float(f1 or 0) - float(f0 or 0)) if f0 is not None and f1 is not None else 999
        try:
            to_l = float(lr1.get("to"))
            snapped = (f1 is not None and f1 >= 0.88) or (
                x1 is not None and wpx and (float(x1) / wpx) >= 0.88
            )
            grew = (to_l - float(lr0.get("to") or 0)) >= 0.9
        except (TypeError, ValueError):
            snapped = True
            grew = False
        x_stable = (f0 is not None and f1 is not None and df <= (1.0 / max(8.0, float((lr1.get("to") or 80) - (lr1.get("from") or 0)) + 1e-6) + 0.02)) or dx <= bs + 2
        ok20 = mid_ok and x_stable and not snapped and grew
        rec(
            "20 last-bar x stable on new 1m bars",
            ok20,
            "x0=%s x1=%s dx=%.2f bs=%.2f w=%s mid=%s last=%s->%s lr0=%s lr1=%s snapped=%s grew=%s" % (
                x0, x1, dx, bs, wpx, mid_ok, last0, s1.get("lastTime"), lr0, lr1, snapped, grew,
            ),
        )
    except Exception as e:
        rec("20 last-bar x stable on new 1m bars", False, str(e))

    try:
        s = snap(page)
        last_t = int(s.get("lastTime") or 0)
        if last_t < 1e9:
            last_t = int(http_json("%s/api/ws/status" % origin, timeout=5).get("last_tick_ts") or 0)
        rows21 = []
        ok21 = True
        for tfv in ("15m", "1h", "4h"):
            info = overlay_fetch(origin, tfv, last_t)
            mb = info["mb"]
            if mb >= 1.5:
                ok21 = False
            rows21.append(
                "%s sec=%.2f mb=%.3f server_ms=%s bars=%s lines=%s marks=%s capped_from=%s" % (
                    tfv, info["sec"], mb, info["server_ms"], info["bars_used"],
                    info["n_lines"], info["n_marks"], info["capped_from"],
                )
            )
        rec("21 1h/4h overlay payload <1.5MB", ok21, str(rows21))
    except Exception as e:
        rec("21 1h/4h overlay payload <1.5MB", False, str(e))


def _view_dump(page):
    s = snap(page)
    sec = tf_sec(s)
    times = [int(t) for t in (s.get("times") or [])[-5:]]
    logs = []
    for rec in (s.get("viewLog") or [])[-30:]:
        logs.append({k: rec.get(k) for k in rec if k != "stack"})
    return {
        "tf": s.get("tf"),
        "mode": s.get("mode"),
        "hist": s.get("historyMode"),
        "last5": [{"t": t, "mod": t % sec} for t in times],
        "grid": s.get("gridErrors") or [],
        "liveCandles": s.get("liveCandleLog") or [],
        "modeLog": s.get("modeLog") or [],
        "viewLog": logs,
        "lastLogical": s.get("lastLogical"),
        "rightLogical": (s.get("lr") or {}).get("to"),
        "leftLogical": (s.get("lr") or {}).get("from"),
        "liveBarDuringDrag": s.get("liveBarDuringDrag"),
        "lastTime": s.get("lastTime"),
        "lastX": s.get("lastX"),
    }


def _drag_chart(page, dx_px, steps=14):
    box = page.locator("#chart").bounding_box()
    x = box["x"] + box["width"] * 0.42
    y = box["y"] + box["height"] * 0.55
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + float(dx_px), y, steps=steps)
    page.mouse.up()
    page.wait_for_timeout(350)


def _drag_bars(page, n_bars):
    s = snap(page)
    bs = float(s.get("barSpacing") or 14)
    _drag_chart(page, float(n_bars) * bs)


def _drag_last_to_mid(page):
    s = snap(page)
    box = page.locator("#chart").bounding_box()
    w = float(box["width"] or 1200)
    x0 = s.get("lastX")
    if x0 is None:
        cmd(page, cmd="lastMid")
        page.wait_for_timeout(400)
        return snap(page)
    target = w * 0.48
    _drag_chart(page, float(x0) - target)
    s1 = snap(page)
    x1 = s1.get("lastX")
    if x1 is None or not (0.22 * w <= float(x1) <= 0.78 * w):
        cmd(page, cmd="lastMid")
        page.wait_for_timeout(400)
        s1 = snap(page)
    return s1


def _inject_new_tf_bars(origin, page, last0, px, tfv, n=2):
    sec = int(TF_SEC.get(tfv) or 60)
    last = int(last0)
    for i in range(1, n + 1):
        ts = last + i * sec
        http_post_json("%s/api/test/tick" % origin, {"price": float(px) + i, "ts": ts})
        t_wait = time.time()
        while time.time() - t_wait < 5:
            cur = snap(page)
            if int(cur.get("lastTime") or 0) >= ts:
                break
            page.wait_for_timeout(120)
        page.wait_for_timeout(150)


def run_view_logic_tests(page, rec, include_fmt=True) -> None:
    parsed = urlparse(page.url)
    origin = "%s://%s" % (parsed.scheme, parsed.netloc)

    def wait_tf_local(tfv, timeout=25):
        t0 = time.time()
        last = None
        need = 40 if tfv in ("1h", "4h") else 100
        while time.time() - t0 < timeout:
            last = snap(page)
            if last.get("tf") == tfv and (last.get("n") or 0) > need and not last.get("tfBusy"):
                return last
            page.wait_for_timeout(150)
        return last

    def run_mid_follow(tfv, name):
        cmd(page, cmd="live")
        page.wait_for_timeout(700)
        cmd(page, cmd="tf", tf=tfv)
        wait_tf_local(tfv, timeout=20)
        _wait_idle(page, 8)
        cmd(page, cmd="lastMid")
        page.wait_for_timeout(550)
        s0 = snap(page)
        f0 = last_frac(s0)
        if f0 is None or not (0.22 <= f0 <= 0.78):
            cmd(page, cmd="lastMid")
            page.wait_for_timeout(550)
            s0 = snap(page)
            f0 = last_frac(s0)
        if f0 is None or not (0.22 <= f0 <= 0.78):
            s0 = _drag_last_to_mid(page)
            f0 = last_frac(s0)
        x0 = s0.get("lastX")
        last0 = int(s0.get("lastTfBucket") or s0.get("lastTime") or 0)
        px = float(s0.get("lastClose") or 100000)
        bs = float(s0.get("barSpacing") or 14)
        lr0 = dict(s0.get("lr") or {})
        vis_w = max(8.0, float((lr0.get("to") or 80) - (lr0.get("from") or 0)))
        mode0 = s0.get("mode")
        _inject_new_tf_bars(origin, page, last0, px, tfv, 2)
        cmd(page, cmd="overlayReload")
        t_ind = time.time()
        while time.time() - t_ind < 8:
            if not snap(page).get("overlayBusy"):
                break
            page.wait_for_timeout(150)
        page.wait_for_timeout(15000)
        s1 = snap(page)
        x1 = s1.get("lastX")
        f1 = last_frac(s1)
        dx = abs(float(x1 or 0) - float(x0 or 0)) if x0 is not None and x1 is not None else 999
        df = abs(float(f1 or 99) - float(f0 or 0))
        snapped = f1 is None or f1 >= 0.88
        mid_ok = f1 is not None and 0.18 <= f1 <= 0.82
        live_ok = str(s1.get("mode") or "") == "LIVE" and not s1.get("historyMode")
        x_stable = df <= max(0.15, 2.01 / vis_w + 0.04) or dx <= (2 * bs + 2)
        ok = mid_ok and x_stable and not snapped and live_ok
        detail = "tf=%s f0=%s f1=%s x0=%s x1=%s dx=%.2f df=%.4f bs=%.2f mode0=%s mode1=%s hist=%s last=%s->%s snapped=%s" % (
            tfv, f0, f1, x0, x1, dx, df, bs, mode0, s1.get("mode"), s1.get("historyMode"),
            last0, s1.get("lastTime"), snapped,
        )
        if not ok:
            detail = detail + " DUMP=" + json.dumps(_view_dump(page), default=str)[:4000]
        rec(name, ok, detail)

    try:
        run_mid_follow("1m", "22 LIVE 1m mid last X stable")
    except Exception as e:
        rec("22 LIVE 1m mid last X stable", False, str(e))
    try:
        run_mid_follow("1h", "23 LIVE 1h mid last X stable")
    except Exception as e:
        rec("23 LIVE 1h mid last X stable", False, str(e))
    try:
        run_mid_follow("15m", "23 LIVE 15m mid last X stable")
    except Exception as e:
        rec("23 LIVE 15m mid last X stable", False, str(e))

    try:
        cmd(page, cmd="live")
        page.wait_for_timeout(700)
        cmd(page, cmd="tf", tf="1m")
        wait_tf_local("1m", timeout=20)
        _wait_idle(page, 8)
        page.wait_for_timeout(400)
        page.evaluate("() => { window.__caLiveBarDuringDrag = false; }")
        box = page.locator("#chart").bounding_box()
        w = float(box["width"] or 1200)
        for _ in range(6):
            _drag_bars(page, 40)
            st = snap(page)
            if st.get("historyMode") or st.get("mode") == "HISTORY":
                break
        st = snap(page)
        dump24 = _view_dump(page)
        hist_ok = bool(st.get("historyMode") or st.get("mode") == "HISTORY")
        cmd(page, cmd="goRt")
        page.wait_for_timeout(700)
        s2 = snap(page)
        x2 = s2.get("lastX")
        f2 = last_frac(s2)
        frac = (float(x2) / w) if x2 is not None and w else 0
        live_ok = str(s2.get("mode") or "") == "LIVE" and not s2.get("historyMode")
        at_right = (f2 is not None and f2 >= 0.72) or (x2 is not None and frac >= 0.72)
        rec(
            "24 HISTORY off-right then >> LIVE",
            hist_ok and live_ok and at_right,
            "hist=%s modeH=%s mode2=%s hist2=%s x2=%s frac=%.2f f2=%s lastL=%s rightL=%s liveBarDrag=%s%s" % (
                hist_ok, st.get("mode"), s2.get("mode"), s2.get("historyMode"), x2, frac, f2,
                dump24.get("lastLogical"), dump24.get("rightLogical"), dump24.get("liveBarDuringDrag"),
                ("" if hist_ok else " DUMP=" + json.dumps(dump24, default=str)[:4000]),
            ),
        )
    except Exception as e:
        rec("24 HISTORY off-right then >> LIVE", False, str(e))

    run_grid_replay_tests(page, rec, origin, wait_tf_local)

    if not include_fmt:
        return
    try:
        rows = []
        ok25 = True
        for tfv in ("1h", "4h"):
            cmd(page, cmd="tf", tf=tfv)
            wait_tf_local(tfv, timeout=20)
            last = int(snap(page).get("lastTime") or 0)
            day = str(page.evaluate("(t) => window.__caTickFmt(t, 2)", last) or "")
            rows.append("%s day=%s" % (tfv, day))
            if ":" in day or not day.isdigit() or not (1 <= int(day) <= 31):
                ok25 = False
        cmd(page, cmd="tf", tf="1m")
        wait_tf_local("1m", timeout=20)
        last = int(snap(page).get("lastTime") or 0)
        hm = str(page.evaluate("(t) => window.__caTickFmt(t, 3)", last) or "")
        rows.append("1m time=%s" % hm)
        if len(hm) != 5 or hm[2] != ":" or not hm.replace(":", "").isdigit():
            ok25 = False
        rec("25 tickMarkFormatter IST year/month/day/time", ok25, str(rows))
    except Exception as e:
        rec("25 tickMarkFormatter IST year/month/day/time", False, str(e))


def _gaps_ok(times, sec):
    xs = [int(t) for t in (times or []) if t is not None]
    if len(xs) < 3:
        return False, "n=%s" % len(xs)
    bad = 0
    missing = 0
    for a, b in zip(xs, xs[1:]):
        g = b - a
        if g == sec:
            continue
        if g > 0 and g % sec == 0:
            missing += 1
            continue
        bad += 1
    return bad == 0, "n=%s bad=%s missing=%s" % (len(xs), bad, missing)


def _wait_idle(page, timeout=12.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = snap(page)
        if not last.get("tfBusy") and not last.get("overlayBusy"):
            return last
        page.wait_for_timeout(80)
    return last or snap(page)


def _replay_15m_cursor(page, wait_tf_local):
    cmd(page, cmd="tf", tf="1m")
    wait_tf_local("1m", timeout=20)
    cmd(page, cmd="start", ts=REPLAY_0937)
    wait_replay(page, at_ts=REPLAY_0937)
    cmd(page, cmd="tf", tf="15m")
    wait_tf_local("15m", timeout=20)
    cmd(page, cmd="replayLayout")
    page.wait_for_timeout(250)
    return _wait_idle(page, 8)


def run_grid_replay_tests(page, rec, origin, wait_tf_local):
    def grid_and_gaps(tfv, mode):
        cmd(page, cmd="addStrat", id="S020")
        page.wait_for_timeout(200)
        cmd(page, cmd="overlayReload")
        _wait_idle(page, 10)
        s = snap(page)
        errs = s.get("gridErrors") or []
        ok_g, det_g = _gaps_ok(s.get("times") or [], TF_SEC[tfv])
        ok = len(errs) == 0 and ok_g
        rec(
            "26 %s %s S020 grid+candle gaps" % (tfv, mode),
            ok,
            "grid=%s gaps=%s last=%s n=%s" % (len(errs), det_g, s.get("lastTime"), s.get("n")),
        )
        return ok

    try:
        cmd(page, cmd="live")
        page.wait_for_timeout(600)
        for tfv in ("15m", "1h"):
            cmd(page, cmd="tf", tf=tfv)
            wait_tf_local(tfv, timeout=20)
            grid_and_gaps(tfv, "LIVE")
        cmd(page, cmd="tf", tf="1m")
        wait_tf_local("1m", timeout=20)
        cmd(page, cmd="start", ts=REPLAY_0937)
        wait_replay(page, at_ts=REPLAY_0937)
        cmd(page, cmd="tf", tf="15m")
        wait_tf_local("15m", timeout=20)
        _wait_idle(page, 12)
        grid_and_gaps("15m", "REPLAY")
        cmd(page, cmd="tf", tf="1h")
        wait_tf_local("1h", timeout=20)
        _wait_idle(page, 10)
        grid_and_gaps("1h", "REPLAY")
    except Exception as e:
        rec("26 15m/1h S020 grid+candle gaps", False, str(e))

    try:
        _replay_15m_cursor(page, wait_tf_local)
        cmd(page, cmd="follow")
        page.wait_for_timeout(250)
        s0 = _wait_idle(page, 8)
        x0 = s0.get("lastX")
        bs = float(s0.get("barSpacing") or 14)
        for _ in range(10):
            cmd(page, cmd="stepCandle")
            page.wait_for_timeout(180)
        s1 = _wait_idle(page, 10)
        x1 = s1.get("lastX")
        dx_c = abs(float(x1 or 0) - float(x0 or 0)) if x0 is not None and x1 is not None else 999
        ok_c = dx_c <= (bs + 2)
        buckets0 = int(s1.get("lastTime") or 0)
        x2 = s1.get("lastX")
        moved = 0
        for _ in range(30):
            prev_last = int(snap(page).get("lastTime") or 0)
            cmd(page, cmd="stepTick")
            page.wait_for_timeout(90)
            st = snap(page)
            if int(st.get("lastTime") or 0) > prev_last:
                moved += 1
        s2 = _wait_idle(page, 8)
        x3 = s2.get("lastX")
        dx_t = abs(float(x3 or 0) - float(x2 or 0)) if x2 is not None and x3 is not None else 999
        ok_t = dx_t <= (bs + 2)
        rec(
            "27 Replay 15m Follow ON Candle/Tick view",
            ok_c and ok_t,
            "dxC=%.2f dxT=%.2f bs=%.2f moved=%s last=%s->%s x0=%s x1=%s" % (
                dx_c, dx_t, bs, moved, buckets0, s2.get("lastTime"), x0, x1,
            ),
        )
    except Exception as e:
        rec("27 Replay 15m Follow ON Candle/Tick view", False, str(e))

    try:
        _replay_15m_cursor(page, wait_tf_local)
        cmd(page, cmd="followOff")
        page.wait_for_timeout(250)
        s0 = _wait_idle(page, 8)
        lr0 = dict(s0.get("lr") or {})
        for _ in range(20):
            cmd(page, cmd="stepTick")
            page.wait_for_timeout(70)
        for _ in range(5):
            cmd(page, cmd="stepCandle")
            page.wait_for_timeout(160)
        s1 = _wait_idle(page, 10)
        lr1 = dict(s1.get("lr") or {})
        df = abs(float(lr0.get("from") or 0) - float(lr1.get("from") or 0))
        dt = abs(float(lr0.get("to") or 0) - float(lr1.get("to") or 0))
        rec(
            "28 Replay 15m Follow OFF view pinned",
            df <= 0.6 and dt <= 0.6,
            "lr0=%s lr1=%s df=%.3f dt=%.3f" % (lr0, lr1, df, dt),
        )
    except Exception as e:
        rec("28 Replay 15m Follow OFF view pinned", False, str(e))

    try:
        _replay_15m_cursor(page, wait_tf_local)
        s0 = _wait_idle(page, 8)
        last0 = int(s0.get("lastTime") or 0)
        cmd(page, cmd="stepCandle")
        page.wait_for_timeout(400)
        s1 = _wait_idle(page, 10)
        part = s1.get("lastPartial") or {}
        bucket = int(part.get("time") or s1.get("lastTime") or 0)
        cap = int(s1.get("replayBarOpen") or 0)
        bars = fetch_1m_range(origin, bucket, cap)
        m1 = [b for b in bars if bucket <= int(b["time"]) <= cap]
        agg_ok = False
        det = "empty"
        if m1:
            o = float(m1[0]["open"])
            h = max(float(b["high"]) for b in m1)
            l = min(float(b["low"]) for b in m1)
            c = float(m1[-1]["close"])
            po, ph, pl, pc = (
                float(part.get("open") or 0),
                float(part.get("high") or 0),
                float(part.get("low") or 0),
                float(part.get("close") or 0),
            )
            agg_ok = (
                abs(po - o) < 1e-6
                and abs(ph - h) < 1e-6
                and abs(pl - l) < 1e-6
                and abs(pc - c) < 1e-6
                and cap < bucket + 900
            )
            future = any(int(b["time"]) > cap for b in m1)
            det = "bucket=%s cap=%s n1m=%s ohlc=(%s,%s,%s,%s) part=(%s,%s,%s,%s) future=%s last0=%s" % (
                bucket, cap, len(m1), o, h, l, c, po, ph, pl, pc, future, last0,
            )
            agg_ok = agg_ok and not future
        rec("29 Candle step partial OHLC = 1m aggregate", agg_ok, det)
    except Exception as e:
        rec("29 Candle step partial OHLC = 1m aggregate", False, str(e))


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


def run_layout_named_test(page, rec, browser, base_url: str) -> None:
    parsed = urlparse(base_url)
    origin = "%s://%s" % (parsed.scheme, parsed.netloc)

    def wait_boot(pg, timeout=20000):
        t0 = time.time()
        while (time.time() - t0) * 1000 < timeout:
            try:
                boot = pg.evaluate(
                    "() => document.documentElement.getAttribute('data-layout-boot')"
                )
            except Exception:
                pg.wait_for_timeout(150)
                continue
            if boot == "1":
                return
            pg.wait_for_timeout(150)
        raise RuntimeError("layout boot did not finish")

    def wait_ids(pg, want_ind, want_strat, timeout=25):
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            last = snap(pg)
            inds = last.get("indIds") or []
            strats = last.get("stratIds") or []
            if all(i in inds for i in want_ind) and all(s in strats for s in want_strat):
                return last
            pg.wait_for_timeout(250)
        return last

    try:
        cmd(page, cmd="clearInds")
        cmd(page, cmd="clearStrats")
        page.wait_for_timeout(400)
        cmd(page, cmd="addInd", id="ULP")
        cmd(page, cmd="addInd", id="ADX")
        cmd(page, cmd="addStrat", id="S022F")
        s_add = wait_ids(page, ("ULP", "ADX"), ("S022F",), timeout=30)
        page.evaluate("async () => { await window.__caLayoutSave('T1'); }")
        cmd(page, cmd="clearInds")
        cmd(page, cmd="clearStrats")
        page.wait_for_timeout(500)
        s_clr = snap(page)
        gone = "ULP" not in (s_clr.get("indIds") or []) and "ADX" not in (s_clr.get("indIds") or [])
        gone = gone and "S022F" not in (s_clr.get("stratIds") or [])
        try:
            page.evaluate("() => window.__caLayoutLoad('T1')")
        except Exception:
            pass
        page.wait_for_load_state("domcontentloaded")
        wait_boot(page)
        wait_ready(page, timeout=45000)
        s_load = wait_ids(page, ("ULP", "ADX"), ("S022F",), timeout=30)
        inds = s_load.get("indIds") or []
        strats = s_load.get("stratIds") or []
        load_ok = gone and "ULP" in inds and "ADX" in inds and "S022F" in strats
        load_ok = load_ok and (s_load.get("layoutName") == "T1")
        last = http_json("%s/api/layouts/_last" % origin, timeout=10)
        last_ok = (last or {}).get("name") == "T1"

        ctx = browser.new_context()
        fresh = ctx.new_page()
        fresh.goto(base_url, wait_until="domcontentloaded")
        wait_boot(fresh)
        wait_ready(fresh, timeout=45000)
        s_auto = wait_ids(fresh, ("ULP", "ADX"), ("S022F",), timeout=30)
        auto_inds = s_auto.get("indIds") or []
        auto_strats = s_auto.get("stratIds") or []
        auto_ok = (
            "ULP" in auto_inds
            and "ADX" in auto_inds
            and "S022F" in auto_strats
            and s_auto.get("layoutName") == "T1"
        )
        ctx.close()
        rec(
            "30 named layout T1 save/load/_last",
            load_ok and last_ok and auto_ok,
            "add=%s gone=%s load_inds=%s load_strats=%s last=%s auto_inds=%s auto_strats=%s auto_name=%s"
            % (
                (s_add or {}).get("indIds"),
                gone,
                inds,
                strats,
                last,
                auto_inds,
                auto_strats,
                s_auto.get("layoutName"),
            ),
        )
    except Exception as e:
        rec("30 named layout T1 save/load/_last", False, str(e))


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
            ok = all(j <= 1500 * 60 for j in jumps)  # zoom may shift a bit; no 1500-bar leap
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
                off_lim = 500 * sec_off if tfv == "15m" else 2 * sec_off
                if abs(Number_or(a0[0]) - Number_or(a1[0])) > off_lim:
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
                    utc_day_change = (int(prev_t) // 86400) != (int(t) // 86400)
                    has_break = utc_day_change or any(prev_t < b <= t for b in breaks)
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
            if not has_gap:
                v_before = pt_map.get(reset_t - 60)
                v_at = pt_map.get(reset_t)
                has_gap = v_before is not None and v_at is not None
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
            cmd(page, cmd="overlayReload")
            page.wait_for_timeout(1500)
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
                    elif int(mx) != cap1 and (key == "candles" or "overlayrsi" in kl or kl == "rsi"):
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
            cmd(page, cmd="lastMid")
            page.wait_for_timeout(400)
            vis = snap(page)
            lr = vis.get("lr") or {}
            try:
                vis_bars = max(80, int(float(lr.get("to")) - float(lr.get("from"))))
            except (TypeError, ValueError):
                vis_bars = 80
            cmd(page, cmd="shift", delta=-(vis_bars + 25))
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
            last_s1 = float(s1.get("lastTime") or 0)
            to_s1 = float((s1.get("vr") or {}).get("to") or 0)
            last_off_right = last_s1 and to_s1 and last_s1 > to_s1 + 2 * 60
            go = bool(s1.get("goRt") or s1.get("historyMode") or s1.get("mode") == "HISTORY" or last_off_right)
            cmd(page, cmd="goRt")
            page.wait_for_timeout(700)
            s2 = snap(page)
            last = float(s2.get("lastTime") or 0)
            to = float((s2.get("vr") or {}).get("to") or 0)
            sec = 60.0
            offset_bars = (to - last) / sec if last else 0
            f2 = last_frac(s2)
            back = str(s2.get("mode") or "") == "LIVE" and last and to >= last - 2 * sec
            pad_ok = (f2 is not None and f2 >= 0.72) or 3 <= offset_bars <= 12 or back
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

        run_overlay_perf_tests(page, rec)
        run_view_logic_tests(page, rec, include_fmt=True)
        run_layout_named_test(page, rec, browser, base_url)

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
        run_overlay_perf_tests(page, rec)
        run_view_logic_tests(page, rec, include_fmt=False)
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
