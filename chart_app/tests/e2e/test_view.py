"""chart_app view + replay e2e (Playwright)."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

IST = ZoneInfo("Asia/Kolkata")
BASE = "http://127.0.0.1:8700/?v=view1"
REPLAY_TS = int(datetime(2026, 10, 5, 9, 30, tzinfo=IST).timestamp())
TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600}


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


def wait_replay(page, timeout=40000):
    t0 = time.time()
    last = None
    while (time.time() - t0) * 1000 < timeout:
        last = snap(page)
        if last and last.get("replayOn") and last.get("replayBarOpen"):
            return last
        page.wait_for_timeout(200)
    raise RuntimeError("replay did not start: %s" % last)


def no_console_errors(errors):
    bad = [e for e in errors if e.get("type") == "error"]
    return bad


def tf_sec(s):
    return TF_SEC.get((s or {}).get("tf") or "1m", 60)


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
                page.wait_for_timeout(1200)
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
            s0 = snap(page)
            n0 = s0.get("n") or 0
            mids = []
            for i in range(3):
                before = snap(page)
                cmd(page, cmd="shift", delta=-80)
                page.wait_for_timeout(900)
                after = snap(page)
                mids.append((before.get("mid"), after.get("mid"), after.get("n")))
            s1 = snap(page)
            # no huge extra jump beyond the commanded shift (~80 bars * 60s)
            ok = True
            for a, b, n in mids:
                if a is None or b is None:
                    ok = False
                    break
                if abs(b - a) > 80 * 60 * 4:
                    ok = False
                    break
            rec("3 loadOlder / pan no view jump", ok, "n0=%s n1=%s mids=%s" % (n0, s1.get("n"), mids))
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

        # 9. Jump to real-time
        try:
            page.locator("#btnReplayLive").click()
            page.wait_for_timeout(2500)
            s = snap(page)
            ok = (not s.get("replayOn")) and s.get("mode") == "LIVE"
            rec("9 Jump to real-time LIVE + WS", ok, "mode=%s ws=%s replay=%s" % (s.get("mode"), s.get("ws"), s.get("replayOn")))
        except Exception as e:
            rec("9 Jump to real-time LIVE + WS", False, str(e))

        cons = no_console_errors(errors)
        rec("console no errors", len(cons) == 0, str(cons[:5]))
        browser.close()
    return results


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else BASE
    rows = run(url)
    failed = [r for r in rows if not r["ok"]]
    print("\n--- summary ---")
    for r in rows:
        print("%s  %s  %s" % ("PASS" if r["ok"] else "FAIL", r["scenario"], r["detail"]))
    sys.exit(1 if failed else 0)
