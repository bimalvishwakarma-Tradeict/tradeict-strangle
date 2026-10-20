#!/usr/bin/env python3
"""Public-only data probe for S025. Small requests, 1s between each. No API keys."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from backtest.strategies.s025_lsoe.parsers import parse_delta_symbol

ROOT = Path(__file__).resolve().parent
DELTA = "https://api.india.delta.exchange"
DERIBIT = "https://www.deribit.com/api/v2"
BINANCE_VISION = "https://data.binance.vision"
TARDIS_SAMPLE = (
    "https://datasets.tardis.dev/v1/deribit/options_chain/2025/06/01/OPTIONS.csv.gz"
)
SLEEP_S = 1.0
TIMEOUT = 30.0
USER_AGENT = "Tradeict-S025-Probe/1.0"
UTC = timezone.utc

logger = logging.getLogger("s025.probe")


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _clip(obj: Any, max_chars: int = 4000) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str, indent=2)
    except TypeError:
        text = str(obj)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... [truncated, len={len(text)}]"
    return text


def _field_paths(obj: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.append(path)
            out.extend(_field_paths(val, path))
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        out.extend(_field_paths(obj[0], prefix + "[]"))
    return out


class ProbeSession:
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self.lines: list[str] = []
        self._first = True

    def close(self) -> None:
        self.client.close()

    def log(self, line: str = "") -> None:
        self.lines.append(line)
        logger.info(line)

    def _pause(self) -> None:
        if self._first:
            self._first = False
            return
        time.sleep(SLEEP_S)

    def get_json(self, url: str) -> tuple[int, Any, str]:
        self._pause()
        try:
            resp = self.client.get(url)
        except httpx.HTTPError as exc:
            return 0, None, f"HTTPError: {exc}"
        body: Any
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = resp.text[:2000]
        return resp.status_code, body, ""

    def head(self, url: str) -> tuple[int, int | None, dict[str, str]]:
        self._pause()
        try:
            resp = self.client.head(url)
        except httpx.HTTPError as exc:
            return 0, None, {"error": str(exc)}
        length = resp.headers.get("content-length")
        size = int(length) if length and length.isdigit() else None
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status_code, size, headers


def _unwrap_result(body: Any) -> Any:
    if isinstance(body, dict) and "result" in body:
        return body["result"]
    return body


def _pick_option(tickers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in tickers:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        ctype = str(row.get("contract_type") or "").lower()
        if ctype in ("call_options", "put_options") or sym.startswith(("C-BTC-", "P-BTC-")):
            return row
    return tickers[0] if tickers else None


def probe_delta_tickers(sess: ProbeSession) -> dict[str, Any] | None:
    qs = urlencode(
        {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": "BTC",
        }
    )
    url = f"{DELTA}/v2/tickers?{qs}"
    sess.log("=== (a) Delta /v2/tickers BTC options ===")
    sess.log(f"GET {url}")
    status, body, err = sess.get_json(url)
    sess.log(f"status={status} err={err or '-'}")
    result = _unwrap_result(body)
    rows = result if isinstance(result, list) else []
    sess.log(f"ticker_count={len(rows)}")
    sample = _pick_option(rows)
    if sample is None:
        sess.log("no option ticker in response")
        sess.log(_clip(body, 1500))
        return None
    sess.log(f"sample_symbol={sample.get('symbol')}")
    sess.log("sample_json:")
    sess.log(_clip(sample))
    fields = _field_paths(sample)
    sess.log("field_list:")
    sess.log(", ".join(fields))
    wanted = [
        "open_interest",
        "oi",
        "greeks",
        "greeks.delta",
        "greeks.gamma",
        "mark_vol",
        "quotes.bid_iv",
        "quotes.ask_iv",
        "best_bid",
        "best_ask",
        "quotes.best_bid",
        "quotes.best_ask",
        "size",
        "bid_size",
        "ask_size",
        "quotes.bid_size",
        "quotes.ask_size",
    ]
    sess.log("wanted_fields_present:")
    for key in wanted:
        present = key in fields or _has_path(sample, key)
        sess.log(f"  {key}: {present}")
    return sample


def _has_path(obj: Any, dotted: str) -> bool:
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _expired_symbol(live: dict[str, Any] | None) -> str:
    if live:
        opt_type, strike, _exp = parse_delta_symbol(str(live.get("symbol") or ""))
        if opt_type and strike:
            prefix = "C" if opt_type == "call" else "P"
            return f"{prefix}-BTC-{int(strike)}-010125"
    return "C-BTC-100000-010125"


def probe_candles(sess: ProbeSession, live: dict[str, Any] | None) -> None:
    sess.log("=== (b) Delta /v2/history/candles prefixes ===")
    expired = _expired_symbol(live)
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    end = int(datetime(2025, 1, 2, tzinfo=UTC).timestamp())
    symbols = [
        f"OI:{expired}",
        "FUNDING:BTCUSD",
        ".DEXBTUSD",
    ]
    for symbol in symbols:
        qs = urlencode(
            {
                "symbol": symbol,
                "resolution": "1h",
                "start": str(start),
                "end": str(end),
            }
        )
        url = f"{DELTA}/v2/history/candles?{qs}"
        sess.log(f"GET {url}")
        status, body, err = sess.get_json(url)
        result = _unwrap_result(body)
        n = len(result) if isinstance(result, list) else 0
        sess.log(f"symbol={symbol} status={status} err={err or '-'} n={n}")
        sample = result[0] if isinstance(result, list) and result else body
        sess.log("sample:")
        sess.log(_clip(sample, 1200))


def probe_trades(sess: ProbeSession, live: dict[str, Any] | None) -> None:
    sess.log("=== (c) Delta public trades lookback ===")
    symbols = ["BTCUSD"]
    if live and live.get("symbol"):
        symbols.append(str(live["symbol"]))
    paths = []
    for sym in symbols:
        paths.append(f"/v2/trades/{sym}")
        paths.append(f"/v2/trades?symbol={sym}")
    oldest_note = "no timestamps parsed"
    oldest_ts: int | None = None
    for path in paths:
        url = f"{DELTA}{path}"
        sess.log(f"GET {url}")
        status, body, err = sess.get_json(url)
        result = _unwrap_result(body)
        rows = result if isinstance(result, list) else []
        sess.log(f"status={status} err={err or '-'} n={len(rows)}")
        if rows:
            sess.log("sample:")
            sess.log(_clip(rows[0], 800))
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = row.get("timestamp") or row.get("time") or row.get("created_at")
            ts_i = _to_unix(raw)
            if ts_i is None:
                continue
            if oldest_ts is None or ts_i < oldest_ts:
                oldest_ts = ts_i
    if oldest_ts is not None:
        age = _now_utc() - datetime.fromtimestamp(oldest_ts, tz=UTC)
        oldest_note = (
            f"oldest_trade_unix={oldest_ts} "
            f"utc={datetime.fromtimestamp(oldest_ts, tz=UTC).isoformat()} "
            f"age={age}"
        )
    sess.log(f"trades_lookback: {oldest_note}")


def _to_unix(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            val /= 1000.0
        return int(val)
    text = str(raw)
    if text.isdigit():
        return _to_unix(int(text))
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def probe_deribit(sess: ProbeSession) -> None:
    url = (
        f"{DERIBIT}/public/get_book_summary_by_currency"
        "?currency=BTC&kind=option"
    )
    sess.log("=== Deribit get_book_summary_by_currency BTC option ===")
    sess.log(f"GET {url}")
    status, body, err = sess.get_json(url)
    sess.log(f"status={status} err={err or '-'}")
    result = _unwrap_result(body)
    rows = result if isinstance(result, list) else []
    sess.log(f"instrument_count={len(rows)}")
    sample = rows[0] if rows else body
    sess.log("sample_json:")
    sess.log(_clip(sample, 2500))
    if isinstance(sample, dict):
        fields = _field_paths(sample)
        sess.log("field_list:")
        sess.log(", ".join(fields))
        for key in (
            "open_interest",
            "mark_iv",
            "bid_price",
            "ask_price",
            "underlying_price",
            "mark_price",
        ):
            sess.log(f"  {key}: {key in sample}")


def _binance_url(kind: str, day: datetime) -> str:
    tag = day.strftime("%Y-%m-%d")
    if kind == "metrics":
        return (
            f"{BINANCE_VISION}/data/futures/um/daily/metrics/"
            f"BTCUSDT/BTCUSDT-metrics-{tag}.zip"
        )
    if kind == "funding":
        return (
            f"{BINANCE_VISION}/data/futures/um/daily/fundingRate/"
            f"BTCUSDT/BTCUSDT-fundingRate-{tag}.zip"
        )
    return (
        f"{BINANCE_VISION}/data/futures/um/daily/aggTrades/"
        f"BTCUSDT/BTCUSDT-aggTrades-{tag}.zip"
    )


def _earliest_ok(sess: ProbeSession, kind: str) -> str:
    candidates = [
        datetime(2019, 9, 1, tzinfo=UTC),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2023, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 9, 1, tzinfo=UTC),
    ]
    first_ok = "not found in probe dates"
    for day in candidates:
        url = _binance_url(kind, day)
        status, size, _headers = sess.head(url)
        sess.log(f"HEAD {url} status={status} size={size}")
        if status == 200:
            if first_ok == "not found in probe dates":
                first_ok = day.strftime("%Y-%m-%d")
    return first_ok


def probe_binance(sess: ProbeSession) -> dict[str, Any]:
    sess.log("=== Binance data.binance.vision HEAD ===")
    sample_day = datetime(2024, 9, 1, tzinfo=UTC)
    out: dict[str, Any] = {}
    for kind in ("metrics", "funding", "aggTrades"):
        url = _binance_url(kind, sample_day)
        status, size, headers = sess.head(url)
        sess.log(
            f"HEAD {kind} {url} status={status} size={size} "
            f"content-type={headers.get('content-type', '')}"
        )
        out[kind] = {"status": status, "size": size, "day": "2024-09-01"}
    sess.log("earliest probe (monthly 1sts):")
    out["metrics_since"] = _earliest_ok(sess, "metrics")
    out["funding_since"] = _earliest_ok(sess, "funding")
    sess.log(f"metrics_since≈{out['metrics_since']} funding_since≈{out['funding_since']}")
    return out


def probe_tardis(sess: ProbeSession) -> tuple[int, int | None]:
    sess.log("=== Tardis HEAD ===")
    sess.log(f"HEAD {TARDIS_SAMPLE}")
    status, size, headers = sess.head(TARDIS_SAMPLE)
    sess.log(
        f"status={status} size={size} "
        f"content-type={headers.get('content-type', '')}"
    )
    return status, size


def write_matrix(
    sess: ProbeSession,
    live: dict[str, Any] | None,
    binance: dict[str, Any],
    tardis: tuple[int, int | None],
) -> None:
    sess.log("=== MATRIX field | live? | history? | kab se | note ===")
    rows = [
        (
            "delta.option.mark_price",
            "yes" if live and live.get("mark_price") is not None else "check json",
            "candles MARK (prior work); this probe not bulk",
            "live now",
            "tickers public",
        ),
        (
            "delta.option.open_interest",
            "yes if field present",
            "try OI: prefix candles",
            "unknown / see (b)",
            "confirm in sample json",
        ),
        (
            "delta.option.greeks",
            "yes if nested greeks",
            "no dedicated history in this probe",
            "live now",
            "delta/gamma in ticker",
        ),
        (
            "delta.option.mark_vol",
            "yes if field present",
            "unknown",
            "live now",
            "IV",
        ),
        (
            "delta.option.quotes.bid_iv/ask_iv",
            "yes if quotes present",
            "unknown",
            "live now",
            "spread IV",
        ),
        (
            "delta.option.best_bid/ask/size",
            "yes if quotes/top-level",
            "unknown",
            "live now",
            "top of book",
        ),
        (
            "delta.candles OI:<expired>",
            "n/a",
            "see (b) status",
            "probe window 2025-01-01",
            "prefix experiment",
        ),
        (
            "delta.candles FUNDING:BTCUSD",
            "n/a",
            "see (b) status",
            "probe window 2025-01-01",
            "prefix experiment",
        ),
        (
            "delta.candles .DEXBTUSD",
            "n/a",
            "see (b) status",
            "probe window 2025-01-01",
            "index/spot-style symbol",
        ),
        (
            "delta.public trades",
            "yes",
            "recent only — see (c) age",
            "see trades_lookback",
            "no API key",
        ),
        (
            "deribit.book_summary oi/mark_iv/bid/ask/underlying",
            "yes",
            "not this endpoint",
            "live now",
            "one call all BTC options",
        ),
        (
            "binance.um.metrics (OI 5m in zip)",
            "n/a",
            f"HEAD status={binance.get('metrics', {}).get('status')}",
            str(binance.get("metrics_since")),
            f"sample 2024-09-01 size={binance.get('metrics', {}).get('size')}",
        ),
        (
            "binance.um.fundingRate daily",
            "n/a",
            f"HEAD status={binance.get('funding', {}).get('status')}",
            str(binance.get("funding_since")),
            f"sample 2024-09-01 size={binance.get('funding', {}).get('size')}",
        ),
        (
            "binance.um.aggTrades daily",
            "n/a",
            f"HEAD status={binance.get('aggTrades', {}).get('status')} (no download)",
            "2024-09-01 sample only",
            f"size={binance.get('aggTrades', {}).get('size')}",
        ),
        (
            "tardis.deribit.options_chain OPTIONS.csv.gz",
            "n/a",
            f"HEAD status={tardis[0]}",
            "sample 2025-06-01",
            f"size={tardis[1]}",
        ),
    ]
    sess.log("field | live? | history? | kab se | note")
    sess.log("-" * 88)
    for field, live_s, hist, since, note in rows:
        sess.log(f"{field} | {live_s} | {hist} | {since} | {note}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    stamp = _now_utc().strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "runs" / f"{stamp}_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "probe.txt"
    sess = ProbeSession()
    sess.log(f"S025 probe start utc={_now_utc().isoformat()}")
    sess.log("public endpoints only — no API keys")
    try:
        live = probe_delta_tickers(sess)
        probe_candles(sess, live)
        probe_trades(sess, live)
        probe_deribit(sess)
        binance = probe_binance(sess)
        tardis = probe_tardis(sess)
        write_matrix(sess, live, binance, tardis)
        sess.log(f"wrote {out_path}")
    except Exception as exc:  # noqa: BLE001 — probe must always write a file
        logger.exception("probe failed")
        sess.log(f"FATAL: {exc}")
    finally:
        sess.close()
        out_path.write_text("\n".join(sess.lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
