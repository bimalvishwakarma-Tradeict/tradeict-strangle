#!/usr/bin/env python3
"""
Probe Delta India GET /v2/history/candles — small empirical checks only.

NO bulk download. Sleep between requests. No print().
Output: console + backtest/results/delta_history_probe.txt
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BASE_URL = "https://api.india.delta.exchange"
CANDLES_PATH = "/v2/history/candles"
PRODUCTS_PATH = "/v2/products"
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "delta_history_probe.txt"
SLEEP_S = 0.75
USER_AGENT = "Tradeict-HistoryProbe/1.0"

logger = logging.getLogger("probe_delta_history")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def _clip(obj: Any, max_chars: int = 1800) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except TypeError:
        s = str(obj)
    if len(s) > max_chars:
        return s[:max_chars] + f"... [truncated, len={len(s)}]"
    return s


def _fmt_ts(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def load_api_creds() -> tuple[str | None, str | None, str]:
    """Load encrypted account keys via existing bot path. Never log secrets."""
    try:
        from backend.core.encryption import decrypt
        from backend.database import SessionLocal
        from backend.models import Account

        db = SessionLocal()
        try:
            account = (
                db.query(Account)
                .filter(Account.is_active.is_(True))
                .order_by(Account.id.asc())
                .first()
            )
            if account is None:
                account = db.query(Account).order_by(Account.id.asc()).first()
            if account is None:
                return None, None, "no Account row in DB"
            key = decrypt(account.api_key_encrypted)
            secret = decrypt(account.api_secret_encrypted)
            if not key or not secret:
                return None, None, "decrypt returned empty key/secret"
            return key, secret, "ok (from encrypted Account)"
        finally:
            db.close()
    except Exception as exc:
        return None, None, f"cred load failed: {type(exc).__name__}: {exc}"


def auth_headers(
    api_key: str, api_secret: str, method: str, path: str, params: dict[str, Any]
) -> dict[str, str]:
    from backend.core.delta_client import DeltaClient

    client = DeltaClient(api_key, api_secret)
    query_string = client._build_query_string(params)
    return client._get_headers(method, path, query_string, "")


def http_get(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
    *,
    headers_extra: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any, str]:
    """Return status, response headers (selected), parsed json or None, raw text snippet."""
    q = urlencode({k: str(v) for k, v in params.items()})
    url = f"{BASE_URL}{path}?{q}" if q else f"{BASE_URL}{path}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers_extra:
        headers.update(headers_extra)
    try:
        resp = client.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        return 0, {}, None, f"HTTPError: {exc}"
    interesting = {
        k: v
        for k, v in resp.headers.items()
        if any(
            x in k.lower()
            for x in ("rate", "limit", "retry", "remain", "reset", "request-id", "cf-")
        )
    }
    text = resp.text
    parsed: Any
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    return resp.status_code, interesting, parsed, text[:2000]


def pick_expired_option_symbol() -> tuple[str, str, int, int]:
    """Return symbol, expiry_iso, min_ts, max_ts from options_trades cache."""
    cache = _BACKTEST / "cache" / "options_trades"
    shards = sorted(cache.glob("opt_trades_*.sqlite"))
    if not shards:
        raise RuntimeError(f"no shards under {cache}")
    conn = sqlite3.connect(shards[0])
    try:
        row = conn.execute(
            "SELECT symbol, expiry, COUNT(*) AS c FROM trades "
            "WHERE symbol LIKE 'C-BTC-%' GROUP BY symbol ORDER BY c DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("no C-BTC symbols in first shard")
        sym, exp, _c = row
        ts = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM trades WHERE symbol=?", (sym,)
        ).fetchone()
        return str(sym), str(exp), int(float(ts[0])), int(float(ts[1]))
    finally:
        conn.close()


def candles_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    result = payload.get("result")
    if isinstance(result, list):
        return len(result)
    return 0


def sample_candles(payload: Any, n: int = 2) -> Any:
    if not isinstance(payload, dict):
        return payload
    result = payload.get("result")
    if isinstance(result, list):
        out = dict(payload)
        out["result"] = result[:n]
        out["_result_len"] = len(result)
        if result:
            out["_first_time"] = result[0].get("time")
            out["_last_time"] = result[-1].get("time")
        return out
    return payload


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "DELTA INDIA HISTORY CANDLES PROBE")
    emit(lines, "=" * 90)
    emit(lines, f"endpoint: GET {BASE_URL}{CANDLES_PATH}")
    emit(lines, f"docs note: max 2000 candles/response; MARK: for mark price; states=expired on /v2/products")
    emit(lines, f"sleep between requests: {SLEEP_S}s")
    emit(lines, "")

    with httpx.Client() as client:
        # -----------------------------------------------------------------
        # PROBE 1: AUTH
        # -----------------------------------------------------------------
        emit(lines, "===== PROBE 1: AUTH =====")
        now = int(time.time())
        # small 1h window, 1h resolution → few candles
        p1_params = {
            "symbol": "BTCUSD",
            "resolution": "1h",
            "start": now - 6 * 3600,
            "end": now - 3600,
        }
        emit(lines, f"request (no auth): params={p1_params}")
        st, hdrs, payload, raw = http_get(client, CANDLES_PATH, p1_params)
        emit(lines, f"status={st}  rate-ish headers={hdrs or '{}'}")
        emit(lines, f"body sample: {_clip(sample_candles(payload) if payload is not None else raw)}")
        public_works = st == 200
        emit(lines, f"VERDICT: public (no API key) works = {public_works}  n_candles={candles_count(payload)}")

        if not public_works:
            emit(lines, "Public failed — trying encrypted Account keys via delta_client auth...")
            api_key, api_secret, msg = load_api_creds()
            emit(lines, f"cred load: {msg}")
            if api_key and api_secret:
                try:
                    ah = auth_headers(api_key, api_secret, "GET", CANDLES_PATH, p1_params)
                    emit(
                        lines,
                        f"auth headers keys: {list(ah.keys())} (api-key/signature redacted in logs)",
                    )
                    time.sleep(SLEEP_S)
                    st2, hdrs2, payload2, raw2 = http_get(
                        client, CANDLES_PATH, p1_params, headers_extra=ah
                    )
                    emit(lines, f"status={st2} headers={hdrs2 or '{}'}")
                    emit(
                        lines,
                        f"body sample: {_clip(sample_candles(payload2) if payload2 is not None else raw2)}",
                    )
                except Exception as exc:
                    emit(lines, f"auth request ERROR: {type(exc).__name__}: {exc}")
            else:
                emit(lines, "No credentials available — cannot retry with auth.")
        emit(lines, "")
        time.sleep(SLEEP_S)

        # -----------------------------------------------------------------
        # PROBE 2: RESOLUTIONS
        # -----------------------------------------------------------------
        emit(lines, "===== PROBE 2: RESOLUTIONS (BTCUSD, ~1 day window) =====")
        resolutions = [
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "12h",
            "1d",
            "1w",
        ]
        # fixed recent closed day window (~24h ending 2h ago)
        end2 = now - 2 * 3600
        start2 = end2 - 24 * 3600
        emit(lines, f"window start={start2} ({_fmt_ts(start2)}) end={end2} ({_fmt_ts(end2)})")
        res_ok: list[str] = []
        res_fail: list[str] = []
        for res in resolutions:
            params = {
                "symbol": "BTCUSD",
                "resolution": res,
                "start": start2,
                "end": end2,
            }
            st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
            n = candles_count(payload)
            err = None
            if isinstance(payload, dict) and payload.get("error"):
                err = payload.get("error")
            ok = st == 200 and n > 0
            if ok:
                res_ok.append(res)
                tag = "OK"
            else:
                res_fail.append(res)
                tag = "FAIL"
            emit(
                lines,
                f"  {res:<4} {tag} status={st} n={n} headers={hdrs or '{}'} "
                f"sample={_clip(sample_candles(payload, 1) if payload is not None else raw, 500)}",
            )
            if err:
                emit(lines, f"       error field: {err}")
            time.sleep(SLEEP_S)
        emit(lines, f"WORKING: {res_ok}")
        emit(lines, f"NOT WORKING / empty: {res_fail}")
        emit(lines, "")

        # -----------------------------------------------------------------
        # PROBE 3: OPTION SYMBOLS
        # -----------------------------------------------------------------
        emit(lines, "===== PROBE 3: OPTION SYMBOLS =====")
        try:
            opt_sym, opt_exp, tmin, tmax = pick_expired_option_symbol()
        except Exception as exc:
            emit(lines, f"ERROR picking symbol: {exc}")
            opt_sym, opt_exp, tmin, tmax = "", "", 0, 0

        emit(lines, f"chosen from cache: symbol={opt_sym} expiry={opt_exp}")
        emit(lines, f"trade ts range in shard: {_fmt_ts(tmin)} -> {_fmt_ts(tmax)}")

        # pick one calendar day in the middle of trade activity (UTC day)
        mid = (tmin + tmax) // 2
        day0 = datetime.fromtimestamp(mid, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_start = int(day0.timestamp())
        day_end = day_start + 24 * 3600 - 1
        emit(lines, f"probe day window: {_fmt_ts(day_start)} -> {_fmt_ts(day_end)}")

        for label, sym in (
            ("(a) plain symbol", opt_sym),
            ("(b) MARK: prefix", f"MARK:{opt_sym}"),
        ):
            params = {
                "symbol": sym,
                "resolution": "1h",
                "start": day_start,
                "end": day_end,
            }
            emit(lines, f"--- {label}: symbol={sym} ---")
            emit(lines, f"params={params}")
            st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
            emit(lines, f"status={st} n={candles_count(payload)} headers={hdrs or '{}'}")
            emit(
                lines,
                f"body sample: {_clip(sample_candles(payload, 3) if payload is not None else raw)}",
            )
            time.sleep(SLEEP_S)

        # If MARK works, count 1m continuity for that day
        mark_sym = f"MARK:{opt_sym}"
        params_1m = {
            "symbol": mark_sym,
            "resolution": "1m",
            "start": day_start,
            "end": day_end,
        }
        emit(lines, f"--- MARK 1m continuity check ({mark_sym}) ---")
        emit(lines, f"params={params_1m}")
        st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params_1m)
        n1m = candles_count(payload)
        emit(lines, f"status={st} n_1m={n1m} (1440 expected if continuous full day)")
        emit(lines, f"headers={hdrs or '{}'}")
        emit(
            lines,
            f"body sample: {_clip(sample_candles(payload, 2) if payload is not None else raw)}",
        )
        if isinstance(payload, dict) and isinstance(payload.get("result"), list) and payload["result"]:
            times = [int(c["time"]) for c in payload["result"] if "time" in c]
            if times:
                gaps = [times[i] - times[i - 1] for i in range(1, len(times))]
                emit(
                    lines,
                    f"time span first->last: {_fmt_ts(times[0])} -> {_fmt_ts(times[-1])}",
                )
                emit(
                    lines,
                    f"gap_sec min/median/max: "
                    f"{min(gaps) if gaps else 'n/a'}/"
                    f"{sorted(gaps)[len(gaps)//2] if gaps else 'n/a'}/"
                    f"{max(gaps) if gaps else 'n/a'}",
                )
                zero_vol = sum(
                    1
                    for c in payload["result"]
                    if float(c.get("volume") or 0) == 0.0
                )
                emit(lines, f"candles with volume==0: {zero_vol}/{n1m}")
        emit(
            lines,
            f"CONTINUOUS? n={n1m} == 1440 → {n1m == 1440} "
            f"(note: API max may cap below 1440; docs say max 2000)",
        )
        emit(lines, "")
        time.sleep(SLEEP_S)

        # -----------------------------------------------------------------
        # PROBE 4: HISTORY DEPTH
        # -----------------------------------------------------------------
        emit(lines, "===== PROBE 4: HISTORY DEPTH =====")
        emit(lines, "--- BTCUSD: try 1y..8y ago (1 day of 1h candles each) ---")
        depth_hits: list[int] = []
        oldest_ok: int | None = None
        for years in range(1, 9):
            end_y = now - years * 365 * 24 * 3600
            start_y = end_y - 24 * 3600
            params = {
                "symbol": "BTCUSD",
                "resolution": "1h",
                "start": start_y,
                "end": end_y,
            }
            emit(lines, f"years_ago={years} window={_fmt_ts(start_y)} -> {_fmt_ts(end_y)}")
            st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
            n = candles_count(payload)
            emit(lines, f"  status={st} n={n} headers={hdrs or '{}'}")
            emit(
                lines,
                f"  sample: {_clip(sample_candles(payload, 2) if payload is not None else raw, 700)}",
            )
            if n > 0 and isinstance(payload, dict):
                depth_hits.append(years)
                first_t = int(payload["result"][0]["time"])
                oldest_ok = first_t if oldest_ok is None else min(oldest_ok, first_t)
            time.sleep(SLEEP_S)

        # If still hitting at 8y, one more tiny step at 10y
        end_y = now - 10 * 365 * 24 * 3600
        start_y = end_y - 24 * 3600
        params = {
            "symbol": "BTCUSD",
            "resolution": "1d",
            "start": start_y,
            "end": end_y,
        }
        emit(lines, f"years_ago=10 (1d) window={_fmt_ts(start_y)} -> {_fmt_ts(end_y)}")
        st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
        n = candles_count(payload)
        emit(lines, f"  status={st} n={n} sample={_clip(sample_candles(payload, 2) if payload else raw, 700)}")
        if n > 0 and isinstance(payload, dict):
            first_t = int(payload["result"][0]["time"])
            oldest_ok = first_t if oldest_ok is None else min(oldest_ok, first_t)
            depth_hits.append(10)
        time.sleep(SLEEP_S)

        emit(lines, f"BTCUSD year-tests with data: {depth_hits}")
        emit(
            lines,
            f"BTCUSD oldest candle time among probes (approx): "
            f"{_fmt_ts(oldest_ok) if oldest_ok else 'UNKNOWN / none'}",
        )

        emit(lines, "--- EXPIRED OPTION: same depth question (CRITICAL) ---")
        if opt_sym:
            # (1) window during life (already know trades exist)
            params = {
                "symbol": f"MARK:{opt_sym}",
                "resolution": "1h",
                "start": day_start,
                "end": day_end,
            }
            st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
            emit(
                lines,
                f"during-life MARK:{opt_sym} status={st} n={candles_count(payload)} "
                f"sample={_clip(sample_candles(payload, 2) if payload else raw, 600)}",
            )
            time.sleep(SLEEP_S)

            # (2) plain during life
            params = {
                "symbol": opt_sym,
                "resolution": "1h",
                "start": day_start,
                "end": day_end,
            }
            st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
            emit(
                lines,
                f"during-life plain {opt_sym} status={st} n={candles_count(payload)} "
                f"sample={_clip(sample_candles(payload, 2) if payload else raw, 600)}",
            )
            time.sleep(SLEEP_S)

            # (3) after expiry — should be empty if no post-expiry history
            try:
                exp_dt = datetime.strptime(opt_exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                exp_dt = day0
            after_start = int((exp_dt + timedelta(days=2)).timestamp())
            after_end = after_start + 24 * 3600
            for sym in (opt_sym, f"MARK:{opt_sym}"):
                params = {
                    "symbol": sym,
                    "resolution": "1h",
                    "start": after_start,
                    "end": after_end,
                }
                st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
                emit(
                    lines,
                    f"AFTER expiry ({_fmt_ts(after_start)}) symbol={sym} "
                    f"status={st} n={candles_count(payload)} "
                    f"sample={_clip(sample_candles(payload, 2) if payload else raw, 500)}",
                )
                time.sleep(SLEEP_S)

            # (4) long before listing — 2y before first trade
            early_end = tmin - 365 * 24 * 3600
            early_start = early_end - 24 * 3600
            for sym in (opt_sym, f"MARK:{opt_sym}"):
                params = {
                    "symbol": sym,
                    "resolution": "1h",
                    "start": early_start,
                    "end": early_end,
                }
                st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
                emit(
                    lines,
                    f"1y BEFORE first trade symbol={sym} "
                    f"status={st} n={candles_count(payload)} "
                    f"sample={_clip(sample_candles(payload, 2) if payload else raw, 500)}",
                )
                time.sleep(SLEEP_S)

            emit(
                lines,
                "CRITICAL Q: expired contract historical candles available? "
                "See during-life vs after-expiry counts above.",
            )
        emit(lines, "")

        # -----------------------------------------------------------------
        # PROBE 5: LIMITS
        # -----------------------------------------------------------------
        emit(lines, "===== PROBE 5: LIMITS =====")
        # ask for ~3 days of 1m (= 4320) to see cap
        end5 = now - 3600
        start5 = end5 - 3 * 24 * 3600
        params = {
            "symbol": "BTCUSD",
            "resolution": "1m",
            "start": start5,
            "end": end5,
        }
        emit(lines, f"request 3d of 1m (~4320 bars expected if uncapped): params={params}")
        st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
        n = candles_count(payload)
        emit(lines, f"status={st} n_returned={n} headers={hdrs or '{}'}")
        emit(lines, f"sample: {_clip(sample_candles(payload, 2) if payload else raw)}")
        emit(lines, f"EMPIRICAL MAX (this call): {n}  (docs claim 2000)")
        time.sleep(SLEEP_S)

        # gentle rate probe: 5 requests at SLEEP_S
        emit(lines, f"rate probe: 5 sequential BTCUSD 1h calls, sleep={SLEEP_S}s")
        for i in range(5):
            params = {
                "symbol": "BTCUSD",
                "resolution": "1h",
                "start": now - (i + 2) * 3600,
                "end": now - (i + 1) * 3600,
            }
            t0 = time.perf_counter()
            st, hdrs, payload, raw = http_get(client, CANDLES_PATH, params)
            dt = time.perf_counter() - t0
            emit(
                lines,
                f"  req#{i+1} status={st} n={candles_count(payload)} "
                f"latency_s={dt:.3f} headers={hdrs or '{}'}",
            )
            time.sleep(SLEEP_S)
        emit(
            lines,
            f"SAFE RATE (conservative from this probe): ~1 req / {SLEEP_S}s "
            f"(no 429 seen). If headers empty, treat docs/unknown.",
        )
        emit(lines, "")

        # -----------------------------------------------------------------
        # PROBE 6: EXPIRED PRODUCTS LIST
        # -----------------------------------------------------------------
        emit(lines, "===== PROBE 6: EXPIRED SYMBOLS LIST =====")
        product_tries = [
            {
                "contract_types": "call_options,put_options",
                "states": "expired",
                "underlying_asset_symbols": "BTC",
                "page_size": 5,
            },
            {
                "contract_types": "call_options",
                "states": "expired,settled",
                "underlying_asset_symbols": "BTC",
                "page_size": 5,
            },
            {
                "contract_types": "call_options,put_options",
                "states": "expired",
                "page_size": 5,
            },
        ]
        for i, params in enumerate(product_tries, 1):
            emit(lines, f"--- products try #{i} params={params} ---")
            st, hdrs, payload, raw = http_get(client, PRODUCTS_PATH, params)
            n = 0
            sample_syms: list[str] = []
            if isinstance(payload, dict):
                result = payload.get("result")
                if isinstance(result, list):
                    n = len(result)
                    for row in result[:5]:
                        if isinstance(row, dict):
                            sample_syms.append(
                                f"{row.get('symbol')}|state={row.get('state')}|"
                                f"exp={row.get('settlement_time') or row.get('expiry_time')}"
                            )
            emit(lines, f"status={st} n_in_page={n} headers={hdrs or '{}'}")
            emit(lines, f"sample symbols: {sample_syms}")
            emit(lines, f"body sample: {_clip(payload if payload is not None else raw, 1200)}")
            # meta/pagination if present
            if isinstance(payload, dict):
                meta = payload.get("meta")
                emit(lines, f"meta: {_clip(meta, 400)}")
            time.sleep(SLEEP_S)

        emit(lines, "")
        emit(lines, "DONE.")

    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
