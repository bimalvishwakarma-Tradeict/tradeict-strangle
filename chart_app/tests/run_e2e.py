#!/usr/bin/env python3
"""Start chart_app server, run Playwright e2e, stop server."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "chart_app" / "server.py"
TEST = ROOT / "chart_app" / "tests" / "e2e" / "test_view.py"
OUT = ROOT / "chart_app" / "tests" / "out"
PORT = int(os.environ.get("CHART_APP_E2E_PORT", "8722"))
URL = "http://127.0.0.1:%s/?v=view1" % PORT
HEALTH = "http://127.0.0.1:%s/" % PORT


def wait_http(url: str, timeout: float = 40.0) -> None:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            with urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise RuntimeError("server did not start: %s" % last)


def _port_up() -> bool:
    try:
        with urlopen(HEALTH, timeout=1.5) as r:
            return r.status < 500
    except Exception:
        return False


def main() -> int:
    try:
        import playwright  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright", "-q"], check=False)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    live = "--live" in sys.argv
    env = os.environ.copy()
    env["CHART_APP_PORT"] = str(PORT)
    if live:
        env.pop("CHART_APP_FIXTURES", None)
        env.pop("CHART_APP_WS_MOCK", None)
    else:
        env["CHART_APP_FIXTURES"] = "1"
        env["CHART_APP_WS_MOCK"] = "1"
    OUT.mkdir(parents=True, exist_ok=True)
    layout_dir = OUT / "layouts"
    if layout_dir.exists():
        shutil.rmtree(layout_dir, ignore_errors=True)
    layout_dir.mkdir(parents=True, exist_ok=True)
    env["CHART_APP_LAYOUTS"] = str(layout_dir)
    log_path = OUT / "server.log"
    if _port_up():
        print("e2e refuse: port %s already in use (will not reuse stale server)" % PORT)
        return 2
    log_f = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_http(HEALTH)
        args = [sys.executable, str(TEST), URL]
        if live:
            args.append("--live")
        r = subprocess.run(args, cwd=str(ROOT))
        return int(r.returncode)
    finally:
        log_f.flush()
        log_f.close()
        if proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    sys.exit(main())
