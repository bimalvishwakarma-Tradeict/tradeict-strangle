# delta_client.py — Delta Exchange India REST API client (auth, orders, chain)

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

# Allow `python -c "from backend.core.delta_client import ..."` from trading-bot/ root
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import DELTA_EXCHANGE_BASE_URL, IST
from backend.core.time_utils import get_dte_label, get_expiry_label_key, get_ist_now

logger = logging.getLogger(__name__)

# Timeouts: orders are critical (fail fast); option chain can be slow
ORDER_TIMEOUT_SECONDS = 10.0
CHAIN_TIMEOUT_SECONDS = 30.0

# Premium targeting for option-chain highlight rows
TARGET_PREMIUM_USD = 150.0
PREMIUM_HIGHLIGHT_RANGE_USD = 20.0
MAX_EXPIRIES_RETURNED = 7


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert API numeric fields (often strings) to float."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_signed_upnl(
    entry_price: float,
    mark_price: float,
    size: float,
    contract_value: float | None = None,
) -> float:
    """
    Correct signed UPNL in USD for Delta India options.

    NEVER use API field `unrealized_pnl` — it returns entry cashflow/notional,
    not actual unrealized P&L.

    Formula: (close_ref - entry_price) × size × contract_value
    where close_ref should be best_offer (ask) for shorts to match Delta
    UI "UPL@Offer". Equiv. for shorts (size < 0):
        (entry_price - best_offer) × abs(size) × contract_value

    The parameter is named mark_price for historical reasons; pass best_offer
    (ask) when matching Delta UPL@Offer.
    """
    from backend.config import OPTIONS_CONTRACT_VALUE

    cv = (
        float(contract_value)
        if contract_value is not None and float(contract_value) > 0
        else float(OPTIONS_CONTRACT_VALUE)
    )
    if entry_price <= 0 or mark_price <= 0 or size == 0 or cv <= 0:
        return 0.0
    return (float(mark_price) - float(entry_price)) * float(size) * cv


def short_leg_realized_pnl(
    entry_fill: float,
    exit_fill: float,
    quantity: int,
    contract_value: float | None = None,
) -> float:
    """Realized USD for closing a short option: (entry - exit) * qty * cv."""
    from backend.config import OPTIONS_CONTRACT_VALUE

    cv = (
        float(contract_value)
        if contract_value is not None and float(contract_value) > 0
        else float(OPTIONS_CONTRACT_VALUE)
    )
    qty = abs(int(quantity))
    return (float(entry_fill) - float(exit_fill)) * qty * cv


def _contract_value_from_position(pos: dict[str, Any]) -> float:
    from backend.config import OPTIONS_CONTRACT_VALUE

    raw = pos.get("contract_value")
    if raw is None and isinstance(pos.get("product"), dict):
        raw = pos["product"].get("contract_value")
    cv = _safe_float(raw, 0.0)
    return cv if cv > 0 else float(OPTIONS_CONTRACT_VALUE)


def _expiry_date_from_ts(expiry_time: Any) -> date | None:
    """Convert product expiry_time (unix seconds) to an IST calendar date."""
    try:
        ts = int(float(expiry_time))
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:  # milliseconds
        ts = ts // 1000
    return datetime.fromtimestamp(ts, tz=IST).date()


def _expiry_from_product(product: dict[str, Any]) -> tuple[date | None, int | None]:
    """
    Extract (expiry_date, unix_ts) from a product.

    Delta India options often expose settlement_time (ISO) instead of expiry_time.
    """
    expiry_time = product.get("expiry_time")
    if expiry_time is not None and expiry_time != "":
        try:
            ts = int(float(expiry_time))
            if ts > 10_000_000_000:
                ts = ts // 1000
            exp_date = datetime.fromtimestamp(ts, tz=IST).date()
            return exp_date, ts
        except (TypeError, ValueError):
            pass

    settlement = product.get("settlement_time")
    if isinstance(settlement, str) and settlement.strip():
        try:
            normalized = settlement.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            else:
                dt = dt.astimezone(IST)
            return dt.date(), int(dt.timestamp())
        except ValueError:
            return None, None
    return None, None


def _extract_delta(payload: dict[str, Any]) -> float:
    """Read delta from nested greeks object or top-level field."""
    greeks = payload.get("greeks")
    if isinstance(greeks, dict):
        return _safe_float(greeks.get("delta"))
    return _safe_float(payload.get("delta"))


def _extract_greek(payload: dict[str, Any], name: str) -> float:
    """Read a named greek from ticker.greeks or top-level."""
    greeks = payload.get("greeks")
    if isinstance(greeks, dict):
        return _safe_float(greeks.get(name))
    return _safe_float(payload.get(name))


def _extract_iv(ticker: dict[str, Any]) -> float:
    """
    Implied vol as a decimal (0.36 = 36%).

    Prefer mid of quotes.bid_iv / ask_iv; fall back to mark_vol.
    """
    quotes = ticker.get("quotes") if isinstance(ticker.get("quotes"), dict) else {}
    bid_iv = _safe_float(quotes.get("bid_iv") or ticker.get("bid_iv"))
    ask_iv = _safe_float(quotes.get("ask_iv") or ticker.get("ask_iv"))
    if bid_iv > 0 and ask_iv > 0:
        return (bid_iv + ask_iv) / 2.0
    if ask_iv > 0:
        return ask_iv
    if bid_iv > 0:
        return bid_iv
    mark_vol = _safe_float(ticker.get("mark_vol") or ticker.get("mark_iv"))
    if mark_vol <= 0:
        return 0.0
    # Some feeds return percent (36.2) instead of decimal (0.362)
    if mark_vol > 5.0:
        return mark_vol / 100.0
    return mark_vol


def _extract_live_quote(ticker: dict[str, Any]) -> tuple[float, float, float, float]:
    """
    Extract (bid, ask, mark_price, delta) from a live /v2/tickers row.

    Delta India puts bid/ask under ticker['quotes'], not top-level best_bid/best_ask.
    If mark_price is missing/zero, fall back to mid = (bid + ask) / 2.
    """
    quotes = ticker.get("quotes") if isinstance(ticker.get("quotes"), dict) else {}
    bid = _safe_float(
        ticker.get("best_bid")
        or ticker.get("bid")
        or quotes.get("best_bid")
        or quotes.get("bid")
    )
    ask = _safe_float(
        ticker.get("best_ask")
        or ticker.get("ask")
        or quotes.get("best_ask")
        or quotes.get("ask")
    )
    mark = _safe_float(
        ticker.get("mark_price")
        or ticker.get("mark")
        or ticker.get("markPrice")
    )
    if mark <= 0 and bid > 0 and ask > 0:
        mark = (bid + ask) / 2.0
    delta = _extract_delta(ticker)
    return bid, ask, mark, delta


def _resolve_product_underlying(underlying: str) -> str:
    """
    Map UI/ticker symbols to options product filter symbol.

    Products use base asset (BTC); perpetual tickers use BTCUSD.
    """
    key = underlying.upper().strip()
    if key.endswith("USD") and len(key) > 3:
        return key[:-3]
    return key


class DeltaAPIError(Exception):
    """Raised when a Delta Exchange API request fails."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Delta API error {status_code}: {message}")


def _premium_diff_pct(call_p: float, put_p: float) -> float:
    if call_p <= 0 or put_p <= 0:
        return 999.0
    return abs(call_p - put_p) / max(call_p, put_p) * 100.0


def select_atm_anchored_pair(
    chain: list[dict[str, Any]],
    spot: float,
    tolerance_pct: float | None = None,
) -> dict[str, Any]:
    """CALL at ATM; PUT = best premium match at or below the ATM strike."""
    if not chain:
        raise DeltaAPIError(
            404, "Empty option chain for ATM-anchored straddle selection"
        )
    if spot <= 0:
        raise DeltaAPIError(400, "Invalid spot price for ATM-anchored straddle")

    atm_row = min(
        chain,
        key=lambda row: abs(_safe_float(row.get("strike")) - float(spot)),
    )
    call_mark = _safe_float(atm_row.get("call_mark_price"))
    if call_mark <= 0:
        raise DeltaAPIError(
            404,
            f"ATM strike {_safe_float(atm_row.get('strike'))} has no call premium",
        )

    atm_strike = _safe_float(atm_row.get("strike"))
    put_candidates: list[tuple[float, float, dict[str, Any]]] = []
    for row in chain:
        strike = _safe_float(row.get("strike"))
        put_mark = _safe_float(row.get("put_mark_price"))
        if strike > atm_strike + 0.01:
            continue
        if put_mark <= 0:
            continue
        prem_diff = abs(put_mark - call_mark)
        put_candidates.append((prem_diff, -strike, row))

    candidates_scanned = len(put_candidates)
    if not candidates_scanned:
        raise DeltaAPIError(
            404,
            f"No put candidates at or below ATM strike {atm_strike}",
        )

    put_candidates.sort(key=lambda t: (t[0], t[1]))
    put_row = put_candidates[0][2]
    put_mark = _safe_float(put_row.get("put_mark_price"))
    diff_pct = _premium_diff_pct(call_mark, put_mark)

    try:
        tolerance = float(tolerance_pct) if tolerance_pct is not None else 25.0
    except (TypeError, ValueError):
        tolerance = 25.0
    tolerance = max(0.0, tolerance)

    if diff_pct > tolerance:
        from backend.core.bot_logger import log_and_buffer

        call_strike = _safe_float(atm_row.get("strike"))
        put_strike = _safe_float(put_row.get("strike"))
        try:
            log_and_buffer(
                "ENTRY_PREMIUM_MISMATCH",
                0,
                {
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "call_premium": round(call_mark, 4),
                    "put_premium": round(put_mark, 4),
                    "diff_pct": round(diff_pct, 2),
                    "tolerance": round(tolerance, 2),
                    "summary": (
                        f"[ENTRY_PREMIUM_MISMATCH] call={call_strike}@"
                        f"{round(call_mark, 2)} put={put_strike}@"
                        f"{round(put_mark, 2)} diff={round(diff_pct, 2)}% "
                        f"tolerance={round(tolerance, 2)}%"
                    ),
                },
            )
        except Exception as exc:
            logger.warning("ENTRY_PREMIUM_MISMATCH log_and_buffer failed: %s", exc)

    return {
        "call_row": atm_row,
        "put_row": put_row,
        "premium_diff_pct": float(diff_pct),
        "candidates_scanned": candidates_scanned,
        "spot_price": float(spot),
    }


class DeltaClient:
    """
    The ONE module that talks to Delta Exchange India REST API.

    BOT TRADE ISOLATION: place_order returns an order id that must be stored
    on Leg.delta_order_id. Never cancel/close an order_id not in our DB.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = DELTA_EXCHANGE_BASE_URL.rstrip("/")
        # Force IPv4 — Delta India API key whitelist is IPv4-only
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=ORDER_TIMEOUT_SECONDS,
        )

    def _generate_signature(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> tuple[str, str]:
        """
        Build HMAC-SHA256 signature for Delta India auth.

        message = method + timestamp + path + query_string + body
        query_string must be "" or "?key=value&key2=value2"
        Secret and message MUST be bytes — never pass raw strings to hmac.new.
        """
        timestamp = str(int(time.time()))
        message = f"{method}{timestamp}{path}{query_string}{body}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return timestamp, signature

    def _get_headers(
        self,
        method: str,
        path: str,
        query_string: str = "",
        body: str = "",
    ) -> dict[str, str]:
        """Return auth headers: api-key, timestamp, signature, Content-Type."""
        timestamp, signature = self._generate_signature(
            method, path, query_string, body
        )
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json",
            "User-Agent": "Tradeict-Short-Strangle-Bot/1.0",
        }

    def _build_query_string(self, params: dict[str, Any] | None) -> str:
        """
        Format query string for signature and URL.

        If params exist: "?key=value&key2=value2"
        If no params: ""
        """
        if not params:
            return ""
        return "?" + urlencode(params, doseq=True)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Signed REST request to Delta Exchange India.

        On success returns response.json()["result"] (not the full envelope).
        On non-200 raises DeltaAPIError.
        """
        query_string = self._build_query_string(params)
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = self._get_headers(method, path, query_string, body_str)
        url = f"{self.base_url}{path}{query_string}"
        request_timeout = timeout if timeout is not None else ORDER_TIMEOUT_SECONDS

        try:
            response = await self.client.request(
                method=method,
                url=url,
                headers=headers,
                content=body_str if body is not None else None,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            logger.error("Delta API timeout on %s %s: %s", method, path, exc)
            raise DeltaAPIError(408, f"Request timed out after {request_timeout}s") from exc
        except httpx.RequestError as exc:
            logger.error("Delta API connection error on %s %s: %s", method, path, exc)
            raise DeltaAPIError(0, f"Connection error: {exc}") from exc

        if response.status_code != 200:
            logger.error(
                "Delta API %s %s failed: %s %s",
                method,
                path,
                response.status_code,
                response.text,
            )
            raise DeltaAPIError(response.status_code, response.text)

        payload = response.json()
        if "result" not in payload:
            raise DeltaAPIError(
                response.status_code,
                f"Missing 'result' in response: {payload}",
            )
        return payload["result"]

    async def test_connection(self) -> dict[str, Any]:
        """GET /v2/profile — returns { account_name, email, id }."""
        result = await self._request("GET", "/v2/profile")
        return {
            "account_name": result.get("nick_name")
            or result.get("username")
            or result.get("account_name")
            or "",
            "email": result.get("email", ""),
            "id": result.get("id"),
        }

    async def get_wallet_balance(self) -> dict[str, float]:
        """
        GET /v2/wallet/balances — return USD/USDT balance summary.

        Delta Exchange India typically returns asset_symbol=USD (not USDT).
        Returns: { balance_usdt, available_balance }  # field name kept for API schema
        """
        result = await self._request("GET", "/v2/wallet/balances")
        balances = result if isinstance(result, list) else result.get("balances", [])

        preferred: dict[str, float] | None = None
        fallback: dict[str, float] | None = None

        for asset in balances:
            symbol = (
                asset.get("asset_symbol")
                or asset.get("symbol")
                or asset.get("currency")
                or ""
            ).upper()
            parsed = {
                "balance_usdt": float(asset.get("balance", 0) or 0),
                "available_balance": float(
                    asset.get("available_balance", asset.get("available", 0)) or 0
                ),
            }
            if symbol == "USDT":
                preferred = parsed
                break
            if symbol == "USD" and fallback is None:
                fallback = parsed

        if preferred is not None:
            return preferred
        if fallback is not None:
            return fallback

        logger.warning("USD/USDT balance not found in wallet balances response")
        return {"balance_usdt": 0.0, "available_balance": 0.0}

    async def get_positions(self) -> list[dict[str, Any]]:
        """
        GET /v2/positions/margined — normalized open positions with MTM fields.

        BOT TRADE ISOLATION: Do NOT iterate this list blindly in the bot loop.
        Use get_mtm_by_product_ids() to match only Leg.product_id values from our DB.
        """
        result = await self._request("GET", "/v2/positions/margined")
        raw: list[Any]
        if isinstance(result, list):
            raw = result
        elif isinstance(result, dict):
            raw = list(
                result.get("positions")
                or result.get("result")
                or []
            )
        else:
            raw = []

        normalized: list[dict[str, Any]] = []
        for pos in raw:
            if not isinstance(pos, dict):
                continue
            product_id = pos.get("product_id") or (pos.get("product") or {}).get("id")
            if product_id is None:
                continue
            upnl = compute_signed_upnl(
                entry_price=_safe_float(pos.get("entry_price")),
                mark_price=_safe_float(pos.get("mark_price")),
                size=_safe_float(pos.get("size")),
                contract_value=_contract_value_from_position(pos),
            )
            normalized.append(
                {
                    "product_id": int(product_id),
                    "symbol": pos.get("symbol")
                    or (pos.get("product") or {}).get("symbol")
                    or "",
                    "size": int(float(pos.get("size") or 0)),
                    "entry_price": _safe_float(pos.get("entry_price")),
                    "mark_price": _safe_float(pos.get("mark_price")),
                    "realized_pnl": _safe_float(pos.get("realized_pnl")),
                    "unrealized_pnl": upnl,
                    "contract_value": _contract_value_from_position(pos),
                    # Raw misleading API field (debug only)
                    "api_unrealized_pnl_raw": _safe_float(pos.get("unrealized_pnl")),
                }
            )
        return normalized

    async def get_option_positions(self) -> list[dict[str, Any]]:
        """
        Open option positions on Delta (non-zero size).

        Returns raw-ish rows with at least product_id, size, product_symbol.
        """
        option_prefixes = (
            "C-BTC",
            "P-BTC",
            "C-ETH",
            "P-ETH",
            "C-XAU",
            "P-XAU",
            "C-DXBT",
            "P-DXBT",
        )
        try:
            result = await self._request("GET", "/v2/positions/margined")
            raw: list[Any]
            if isinstance(result, list):
                raw = result
            elif isinstance(result, dict):
                raw = list(
                    result.get("positions")
                    or result.get("result")
                    or result.get("data")
                    or []
                )
            else:
                raw = []

            option_positions: list[dict[str, Any]] = []
            for pos in raw:
                if not isinstance(pos, dict):
                    continue
                product = pos.get("product") if isinstance(pos.get("product"), dict) else {}
                symbol = str(
                    pos.get("product_symbol")
                    or pos.get("symbol")
                    or product.get("symbol")
                    or ""
                )
                size = float(pos.get("size") or 0)
                if size == 0:
                    continue
                if not any(prefix in symbol for prefix in option_prefixes):
                    continue
                product_id = pos.get("product_id") or product.get("id")
                row = dict(pos)
                row["product_symbol"] = symbol
                row["size"] = size
                if product_id is not None:
                    try:
                        row["product_id"] = int(product_id)
                    except (TypeError, ValueError):
                        pass
                option_positions.append(row)
            return option_positions
        except Exception as exc:
            logger.warning("Could not fetch option positions: %s", exc)
            return []

    async def verify_position_exists(self, product_id: int) -> bool:
        """Return True if Delta has a non-zero size position for product_id."""
        try:
            wanted = int(product_id)
            result = await self._request("GET", "/v2/positions/margined")
            raw: list[Any]
            if isinstance(result, list):
                raw = result
            elif isinstance(result, dict):
                raw = list(
                    result.get("positions")
                    or result.get("result")
                    or result.get("data")
                    or []
                )
            else:
                raw = []

            for pos in raw:
                if not isinstance(pos, dict):
                    continue
                product = (
                    pos.get("product") if isinstance(pos.get("product"), dict) else {}
                )
                pid = pos.get("product_id") or product.get("id")
                try:
                    if int(pid) != wanted:
                        continue
                except (TypeError, ValueError):
                    continue
                size = float(pos.get("size") or 0)
                return size != 0
            return False
        except Exception as exc:
            logger.warning("verify_position_exists failed: %s", exc)
            return False

    @staticmethod
    def _extract_unrealized_pnl(pos: dict[str, Any]) -> float:
        """
        Fallback UPNL from position row alone (mark only).

        Prefer get_positions_upnl() which uses best_offer (ask) to match
        Delta UI UPL@Offer. This mark-based path is only for list/normalize
        helpers that cannot fetch tickers per row.
        """
        return compute_signed_upnl(
            entry_price=_safe_float(pos.get("entry_price")),
            mark_price=_safe_float(pos.get("mark_price")),
            size=_safe_float(pos.get("size")),
            contract_value=_contract_value_from_position(pos),
        )

    async def get_positions_upnl(
        self, product_ids: list[int] | None = None
    ) -> dict[int, dict[str, Any]]:
        """
        Get correct UPNL matching Delta UI "UPL@Offer".

        Delta India `unrealized_pnl` field is entry cashflow — NEVER use it.

        Correct short-option UPNL (matches Delta UPL@Offer):
            (entry_price - best_offer) × abs(size) × contract_size
        where best_offer = L2 top ask / ticker quotes.best_ask
        (the price paid to buy back the short). Falls back to mark only
        if no offer is available.
        """
        wanted: set[int] | None = None
        if product_ids:
            wanted = {int(pid) for pid in product_ids}
            if not wanted:
                return {}

        try:
            result = await self._request("GET", "/v2/positions/margined")
        except Exception:
            result = await self._request("GET", "/v2/positions")

        raw: list[Any]
        if isinstance(result, list):
            raw = result
        elif isinstance(result, dict):
            raw = list(
                result.get("positions")
                or result.get("result")
                or result.get("data")
                or []
            )
        else:
            raw = []

        out: dict[int, dict[str, Any]] = {}
        for pos in raw:
            if not isinstance(pos, dict):
                continue
            product = pos.get("product") if isinstance(pos.get("product"), dict) else {}
            product_id = pos.get("product_id") or product.get("id")
            if product_id is None:
                continue
            pid = int(product_id)
            if wanted is not None and pid not in wanted:
                continue

            entry = _safe_float(pos.get("entry_price"))
            mark = _safe_float(pos.get("mark_price"))
            size = _safe_float(pos.get("size"))
            cv = _contract_value_from_position(pos)
            symbol = str(
                pos.get("product_symbol")
                or pos.get("symbol")
                or product.get("symbol")
                or ""
            )
            api_raw = _safe_float(pos.get("unrealized_pnl"))

            # Debug: discover which price/offer fields Delta returns on positions
            logger.debug(
                "Position fields: %s",
                list(pos.keys()),
            )
            logger.debug(
                "Position price/offer fields: %s",
                {
                    k: v
                    for k, v in pos.items()
                    if any(
                        x in str(k).lower()
                        for x in ("price", "offer", "ask", "bid", "mark")
                    )
                },
            )

            # Prefer live best_offer (ask) from L2/ticker — matches Delta UPL@Offer
            best_offer = 0.0
            # First try any ask/offer fields on the position payload itself
            for field in (
                "best_ask_price",
                "ask_price",
                "best_offer",
                "best_ask",
            ):
                candidate = _safe_float(pos.get(field))
                if candidate > 0:
                    best_offer = candidate
                    break

            if best_offer <= 0 and symbol and size < 0:
                try:
                    best_offer = float(await self.get_short_exit_price(symbol))
                except Exception as exc:
                    logger.warning(
                        "Best offer fetch failed pid=%s symbol=%s: %s",
                        pid,
                        symbol,
                        exc,
                    )
            elif best_offer <= 0 and symbol and size > 0:
                try:
                    best_offer = float(await self.get_long_exit_price(symbol))
                except Exception as exc:
                    logger.warning(
                        "Best bid fetch failed pid=%s symbol=%s: %s",
                        pid,
                        symbol,
                        exc,
                    )

            # UPL@Offer: use best_offer; fall back to mark only if no offer
            close_price = best_offer if best_offer > 0 else mark
            if entry > 0 and close_price > 0 and size != 0 and cv > 0:
                upnl = compute_signed_upnl(entry, close_price, size, cv)
            else:
                upnl = 0.0

            logger.info(
                "UPL@Offer calc: %s entry=%.4f offer=%.4f mark=%.4f "
                "size=%s cv=%s upnl=%.4f (api_unrealized_pnl=%.4f IGNORED)",
                symbol or pid,
                entry,
                close_price,
                mark,
                size,
                cv,
                upnl,
                api_raw,
            )

            out[pid] = {
                "upnl": round(float(upnl), 4),
                "entry_price": entry,
                "mark_price": mark,
                "best_offer": float(close_price),
                "size": size,
                "symbol": symbol,
                "contract_value": cv,
                "api_unrealized_pnl_raw": api_raw,
            }

        if wanted is not None:
            missing = wanted - set(out.keys())
            if missing:
                logger.warning(
                    "get_positions_upnl missing product_ids=%s found=%s",
                    sorted(missing),
                    sorted(out.keys()),
                )
        return out

    async def get_mtm_by_product_ids(
        self, product_ids: list[int]
    ) -> dict[int, float]:
        """
        product_id → UPNL USD matching Delta UI UPL@Offer.

        Uses best_offer (ask), not mark_price:
            (entry - best_offer) × abs(size) × contract_size  (shorts)
        """
        detailed = await self.get_positions_upnl(product_ids)
        return {pid: float(row["upnl"]) for pid, row in detailed.items()}

    async def place_order(
        self,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        time_in_force: str = "ioc",
        bracket_stop_loss_price: float | None = None,
        bracket_stop_loss_limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """
        POST /v2/orders — place an order (10s timeout).

        Returns: { order_id, status, avg_fill_price, size }
        Caller MUST store order_id on Leg.delta_order_id (isolation rule).
        """
        body = {
            "product_id": product_id,
            "size": size,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
        }
        if reduce_only:
            body["reduce_only"] = True

        # Delta "bracket" stop-loss: attach stop-loss directly to entry order.
        # Bracket SL confirmed working on Delta Exchange India
        # Format: bracket_stop_loss_price + bracket_stop_loss_limit_price
        # Bracket auto-cancels when position is closed (any reason)
        # No orphan stop orders remain after trade exit.
        if bracket_stop_loss_price is not None:
            stop_px = round(float(bracket_stop_loss_price), 2)
            if stop_px > 0:
                body["bracket_stop_loss_price"] = str(stop_px)
                if bracket_stop_loss_limit_price is not None:
                    limit_px = round(float(bracket_stop_loss_limit_price), 2)
                else:
                    # For SHORT entry (side="sell"), the bracket SL is a BUY order.
                    # Default limit is 5% above the stop trigger.
                    limit_px = round(stop_px * 1.05, 2)
                body["bracket_stop_loss_limit_price"] = str(limit_px)

        result = await self._request(
            "POST",
            "/v2/orders",
            body=body,
            timeout=ORDER_TIMEOUT_SECONDS,
        )
        return {
            "order_id": result.get("id") or result.get("order_id"),
            "status": result.get("state") or result.get("status"),
            "avg_fill_price": float(
                result.get("average_fill_price")
                or result.get("avg_fill_price")
                or result.get("fill_price")
                or 0
            ),
            "size": int(result.get("size", size) or size),
            "raw": result if isinstance(result, dict) else {},
        }

    async def edit_bracket_order(
        self,
        order_id: int | str,
        product_id: int,
        bracket_stop_loss_price: float,
        bracket_stop_loss_limit_price: float | None = None,
    ) -> dict[str, Any]:
        """
        PUT /v2/orders/bracket — amend bracket SL attached to an order/position.

        After an IOC market fill the parent order is often no longer editable;
        callers must treat failure as non-fatal and keep the provisional price.
        """
        stop_px = round(float(bracket_stop_loss_price), 2)
        limit_px = (
            round(float(bracket_stop_loss_limit_price), 2)
            if bracket_stop_loss_limit_price is not None
            else round(stop_px * 1.05, 2)
        )
        body = {
            "id": int(order_id),
            "product_id": int(product_id),
            "bracket_stop_loss_price": str(stop_px),
            "bracket_stop_loss_limit_price": str(limit_px),
        }
        result = await self._request(
            "PUT",
            "/v2/orders/bracket",
            body=body,
            timeout=ORDER_TIMEOUT_SECONDS,
        )
        return result if isinstance(result, dict) else {"result": result}

    async def get_open_stop_orders(self) -> list[dict[str, Any]]:
        """
        Fetch open stop-loss orders for this account.

        Used for one-time orphan cleanup after deploying bracket orders.
        """
        result = await self._request(
            "GET",
            "/v2/orders",
            params={
                "state": "open",
                "stop_order_type": "stop_loss_order",
            },
        )
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict)]
        if isinstance(result, dict):
            # Some Delta deployments return a wrapper envelope under `orders` or `result`.
            maybe = result.get("orders") or result.get("result") or []
            if isinstance(maybe, list):
                return [r for r in maybe if isinstance(r, dict)]
        return []

    async def get_order(self, order_id: int | str) -> dict[str, Any]:
        """
        GET /v2/orders/{order_id} — fetch order details including fill price.
        """
        result = await self._request(
            "GET",
            f"/v2/orders/{order_id}",
            timeout=ORDER_TIMEOUT_SECONDS,
        )
        return result if isinstance(result, dict) else {}

    async def resolve_fill_price(
        self,
        order_result: dict[str, Any],
        symbol_for_fallback: str | None = None,
    ) -> float:
        """
        Resolve fill price from place_order result, then get_order.

        Returns 0.0 if unresolved — caller should fall back to mark price.
        Does not raise; missing fill fields must not abort trade registration.
        """
        price_fields = (
            "average_fill_price",
            "avg_fill_price",
            "fill_price",
            "limit_price",
        )

        def _positive(val: Any) -> float | None:
            if val is None or val == "":
                return None
            try:
                price = float(val)
            except (TypeError, ValueError):
                return None
            return price if price > 0 else None

        for field in price_fields:
            found = _positive(order_result.get(field))
            if found is not None:
                return found

        raw = order_result.get("raw")
        if isinstance(raw, dict):
            for field in price_fields:
                found = _positive(raw.get(field))
                if found is not None:
                    return found

        order_id = order_result.get("order_id") or order_result.get("id")
        if order_id is not None:
            try:
                fetched = await self.get_order(order_id)
                for field in price_fields:
                    found = _positive(fetched.get(field))
                    if found is not None:
                        return found
                raw_fetched = fetched.get("raw") if isinstance(fetched, dict) else None
                if isinstance(raw_fetched, dict):
                    for field in price_fields:
                        found = _positive(raw_fetched.get(field))
                        if found is not None:
                            return found
            except Exception as exc:
                logger.warning("Could not fetch order %s for fill price: %s", order_id, exc)

        # Optional mark fallback when caller passes symbol (kept for executor path)
        if symbol_for_fallback:
            try:
                mark = float(await self.get_mark_price(symbol_for_fallback))
                if mark > 0:
                    logger.warning(
                        "Using mark price as fill for %s: %s",
                        symbol_for_fallback,
                        mark,
                    )
                    return mark
            except Exception as exc:
                logger.warning(
                    "mark_price fallback failed for %s: %s",
                    symbol_for_fallback,
                    exc,
                )

        logger.error("Could not resolve fill price for order: %s", order_result)
        return 0.0

    async def get_btc_index_price(self) -> float:
        """BTC index/spot for options fee notional (prefer spot, then mark)."""
        ticker = await self.get_ticker("BTCUSD")
        for key in ("spot_price", "mark_price", "close", "open"):
            try:
                val = float(ticker.get(key) or 0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                return val
        quotes = ticker.get("quotes") if isinstance(ticker.get("quotes"), dict) else {}
        try:
            bid = float(quotes.get("best_bid") or 0)
            ask = float(quotes.get("best_ask") or 0)
        except (TypeError, ValueError):
            bid, ask = 0.0, 0.0
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else max(bid, ask)
        if mid > 0:
            return mid
        raise DeltaAPIError(0, "BTCUSD index/spot price unavailable")

    async def get_order_commission(self, order_id: int | str) -> float:
        """
        Actual trading fee (inc. GST) for an order.

        Prefer order.paid_commission; else sum fill.commission for that order_id.
        Delta commission fields already include GST.
        """
        oid = str(order_id)
        # 1) Order object
        try:
            order = await self.get_order(oid)
            for field in ("paid_commission", "commission"):
                try:
                    val = abs(float(order.get(field) or 0))
                except (TypeError, ValueError):
                    val = 0.0
                if val > 0:
                    return val
        except Exception as exc:
            logger.warning("get_order_commission order fetch failed id=%s: %s", oid, exc)

        # 2) Sum fills (handles partial fills)
        try:
            fills = await self._request(
                "GET",
                "/v2/fills",
                params={"page_size": 50},
            )
        except Exception as exc:
            logger.warning("get_order_commission fills fetch failed: %s", exc)
            return 0.0

        total = 0.0
        if isinstance(fills, list):
            for fill in fills:
                if str(fill.get("order_id") or "") != oid:
                    continue
                try:
                    total += abs(float(fill.get("commission") or 0))
                except (TypeError, ValueError):
                    continue
                # Prefer meta total if present (already full)
                meta = fill.get("meta_data") if isinstance(fill.get("meta_data"), dict) else {}
                try:
                    meta_total = abs(
                        float(meta.get("total_commission_in_settling_asset") or 0)
                    )
                except (TypeError, ValueError):
                    meta_total = 0.0
                if meta_total > 0:
                    # meta is per-fill total; use commission field sum instead
                    pass
        return float(total)

    async def get_product_exit_fill_since(
        self,
        *,
        product_id: int,
        since: Any = None,
        side: str | None = None,
        is_long: bool = False,
    ) -> float | None:
        """
        Best-effort exit fill for a product from /v2/fills.

        Prefers fills matching the close side (buy for short, sell for long)
        after ``since``. Returns None when no usable fill is found — never 0.0.
        """
        close_side = str(side or ("sell" if is_long else "buy")).lower()
        since_ts: float | None = None
        if since is not None:
            try:
                if hasattr(since, "timestamp"):
                    since_ts = float(since.timestamp())
                else:
                    since_ts = float(since)
            except (TypeError, ValueError):
                since_ts = None

        try:
            fills = await self._request(
                "GET",
                "/v2/fills",
                params={
                    "product_id": int(product_id),
                    "page_size": 100,
                },
            )
        except Exception as exc:
            logger.warning(
                "get_product_exit_fill_since product=%s failed: %s",
                product_id,
                exc,
            )
            return None

        rows: list[Any]
        if isinstance(fills, list):
            rows = fills
        elif isinstance(fills, dict):
            maybe = fills.get("result") or fills.get("fills") or []
            rows = list(maybe) if isinstance(maybe, list) else []
        else:
            rows = []

        best_px: float | None = None
        best_ts = -1.0
        for fill in rows:
            if not isinstance(fill, dict):
                continue
            try:
                fpid = int(fill.get("product_id") or 0)
            except (TypeError, ValueError):
                fpid = 0
            if fpid and fpid != int(product_id):
                continue
            fside = str(fill.get("side") or "").lower()
            if fside and fside != close_side:
                continue
            try:
                px = float(
                    fill.get("price")
                    or fill.get("fill_price")
                    or fill.get("avg_fill_price")
                    or 0
                )
            except (TypeError, ValueError):
                px = 0.0
            if px <= 0:
                continue
            ts_raw = (
                fill.get("created_at")
                or fill.get("timestamp")
                or fill.get("fill_time")
            )
            ts = -1.0
            if ts_raw is not None:
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = float(ts_raw)
                        if ts > 1e12:
                            ts = ts / 1000.0
                    else:
                        from datetime import datetime

                        ts = datetime.fromisoformat(
                            str(ts_raw).replace("Z", "+00:00")
                        ).timestamp()
                except Exception:
                    ts = -1.0
            if since_ts is not None and ts > 0 and ts < since_ts - 1.0:
                continue
            if ts >= best_ts:
                best_ts = ts
                best_px = px
        return best_px

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        """
        DELETE /v2/orders/{order_id}

        BOT TRADE ISOLATION: Only call with order_ids stored in our DB.
        """
        result = await self._request(
            "DELETE",
            f"/v2/orders/{order_id}",
            timeout=ORDER_TIMEOUT_SECONDS,
        )
        return result if isinstance(result, dict) else {"result": result}

    async def place_stop_order(
        self,
        product_id: int,
        size: int,
        side: str,
        stop_price: float,
    ) -> dict[str, Any]:
        """
        DEPRECATED — DO NOT CALL from production paths.

        Standalone stop orders orphan when the position closes (no reduce_only)
        and can open unwanted positions later. Attach bracket_stop_loss_price /
        bracket_stop_loss_limit_price on the entry order instead.

        Kept only so orphan-cleanup / historical tooling can still reference the
        API shape. New code must never call this.
        """
        stop_px = round(float(stop_price), 2)
        if str(side).lower() == "buy":
            limit_price = round(stop_px * 1.05, 2)
        else:
            limit_price = round(stop_px * 0.95, 2)

        body: dict[str, Any] = {
            "product_id": int(product_id),
            "size": abs(int(size)),
            "side": side,
            "order_type": "limit_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": str(stop_px),
            "limit_price": str(limit_price),
            "time_in_force": "gtc",
        }
        logger.info(
            "Placing stop order: product=%s side=%s stop=%s limit=%s body=%s",
            product_id,
            side,
            stop_px,
            limit_price,
            body,
        )
        result = await self._request(
            "POST",
            "/v2/orders",
            body=body,
            timeout=ORDER_TIMEOUT_SECONDS,
        )
        raw = result if isinstance(result, dict) else {"result": result}
        logger.info(
            "Stop order placed: id=%s state=%s",
            raw.get("id"),
            raw.get("state"),
        )
        # Normalize for delta_sl callers while preserving raw API fields
        return {
            **raw,
            "order_id": raw.get("id") or raw.get("order_id"),
            "status": raw.get("state") or raw.get("status"),
            "stop_price": float(stop_px),
            "limit_price": float(limit_price),
            "size": int(raw.get("size", size) or size),
            "raw": raw,
        }

    async def modify_stop_order(
        self,
        order_id: int,
        new_stop_price: float,
    ) -> dict[str, Any]:
        """
        Cancel existing stop order. Caller must place a replacement.

        Delta does not reliably support in-place stop modify for options.
        """
        try:
            await self.cancel_order(int(order_id))
            return {"cancelled": True, "order_id": int(order_id)}
        except Exception as exc:
            logger.warning(
                "Could not cancel SL order %s for modify (stop=%.2f): %s",
                order_id,
                new_stop_price,
                exc,
            )
            return {"cancelled": False, "order_id": int(order_id), "error": str(exc)}

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """GET /v2/tickers/{symbol} — full ticker including mark/bid/ask."""
        result = await self._request("GET", f"/v2/tickers/{symbol}")
        return result if isinstance(result, dict) else {}

    async def get_mark_price(self, symbol: str) -> float:
        """
        Return live mark_price for symbol from /v2/tickers/{symbol}.

        Fallback: mid = (best_bid + best_ask) / 2 when mark is missing/zero.
        """
        ticker = await self.get_ticker(symbol)
        _bid, _ask, mark, _delta = _extract_live_quote(ticker)
        if mark <= 0:
            raise DeltaAPIError(0, f"mark_price missing for symbol {symbol}")
        return mark

    async def get_l2_top_of_book(self, symbol: str) -> tuple[float, float]:
        """
        Return (best_bid, best_ask) from L2 top of book.

        Raises DeltaAPIError if either side is missing.
        """
        book = await self._request("GET", f"/v2/l2orderbook/{symbol}")
        if not isinstance(book, dict):
            raise DeltaAPIError(0, f"L2 book missing for {symbol}")
        buys = book.get("buy") or book.get("bids") or []
        sells = book.get("sell") or book.get("asks") or []
        bid = 0.0
        ask = 0.0
        if buys and isinstance(buys[0], dict):
            bid = _safe_float(buys[0].get("price"))
        elif buys and isinstance(buys[0], (list, tuple)) and len(buys[0]) >= 1:
            bid = _safe_float(buys[0][0])
        if sells and isinstance(sells[0], dict):
            ask = _safe_float(sells[0].get("price"))
        elif sells and isinstance(sells[0], (list, tuple)) and len(sells[0]) >= 1:
            ask = _safe_float(sells[0][0])
        if bid <= 0 or ask <= 0:
            raise DeltaAPIError(0, f"L2 top missing bid/ask for {symbol}")
        return bid, ask

    async def get_short_exit_price(self, symbol: str) -> float:
        """
        Best offer to buy back a short — matches Delta "Best Offer" / UPL @offer.

        Prefer L2 top ask, then ticker quotes.best_ask.
        Never returns mark (mark causes large UPNL drift vs Delta UI).
        """
        # 1) L2 order book top of book (sell side = offer)
        try:
            book = await self._request("GET", f"/v2/l2orderbook/{symbol}")
            if isinstance(book, dict):
                sells = book.get("sell") or book.get("asks") or []
                if sells and isinstance(sells[0], dict):
                    l2_ask = _safe_float(sells[0].get("price"))
                    if l2_ask > 0:
                        return l2_ask
        except Exception as exc:
            logger.debug("L2 ask fetch failed for %s: %s", symbol, exc)

        # 2) Ticker quotes.best_ask only — do not use mark
        ticker = await self.get_ticker(symbol)
        _bid, ask, _mark, _delta = _extract_live_quote(ticker)
        if ask > 0:
            return ask
        raise DeltaAPIError(
            0, f"best offer (ask) missing for symbol {symbol} — refusing mark fallback"
        )

    async def get_long_exit_price(self, symbol: str) -> float:
        """Best bid to sell a long — prefer L2 top bid, then ticker bid, then mark."""
        try:
            book = await self._request("GET", f"/v2/l2orderbook/{symbol}")
            if isinstance(book, dict):
                buys = book.get("buy") or book.get("bids") or []
                if buys and isinstance(buys[0], dict):
                    l2_bid = _safe_float(buys[0].get("price"))
                    if l2_bid > 0:
                        return l2_bid
        except Exception as exc:
            logger.debug("L2 bid fetch failed for %s: %s", symbol, exc)

        ticker = await self.get_ticker(symbol)
        bid, ask, mark, _delta = _extract_live_quote(ticker)
        if bid > 0:
            return bid
        if mark > 0:
            return mark
        if ask > 0:
            return ask
        raise DeltaAPIError(0, f"long exit price missing for symbol {symbol}")

    async def get_underlying_price(self, underlying_symbol: str) -> float:
        """
        GET /v2/tickers/{underlying_symbol} for perpetual / spot mark price.

        Example: underlying_symbol='BTCUSD' → used as payoff graph center.
        """
        ticker = await self.get_ticker(underlying_symbol.upper())
        mark = _safe_float(
            ticker.get("mark_price")
            or ticker.get("close")
            or ticker.get("spot_price")
        )
        if mark <= 0:
            _bid, _ask, mark, _delta = _extract_live_quote(ticker)
        if mark <= 0:
            raise DeltaAPIError(
                0, f"underlying mark_price missing for {underlying_symbol}"
            )
        return mark

    async def _get_products(
        self,
        contract_type: str,
        underlying: str,
    ) -> list[dict[str, Any]]:
        """
        GET /v2/products for live options of one contract type.

        Symbol format examples:
          Call: C-BTC-45000-231215
          Put:  P-BTC-38000-231215
        """
        params = {
            "contract_type": contract_type,
            "underlying_asset_symbol": underlying.upper(),
            "state": "live",
        }
        result = await self._request(
            "GET",
            "/v2/products",
            params=params,
            timeout=CHAIN_TIMEOUT_SECONDS,
        )
        if isinstance(result, list):
            products = result
        elif isinstance(result, dict):
            products = list(result.get("products", []))
        else:
            products = []

        # Hard-filter: Delta sometimes returns mixed underlyings
        wanted = underlying.upper()
        filtered: list[dict[str, Any]] = []
        for product in products:
            asset = product.get("underlying_asset") or {}
            asset_symbol = str(
                asset.get("symbol")
                or product.get("underlying_asset_symbol")
                or ""
            ).upper()
            if asset_symbol == wanted or asset_symbol.startswith(wanted):
                # Also require contract_type match when present
                ctype = str(product.get("contract_type") or "").lower()
                if ctype and ctype != contract_type:
                    continue
                filtered.append(product)
        return filtered

    async def _get_option_tickers_map(self, underlying: str) -> dict[str, dict[str, Any]]:
        """
        Fetch live option tickers for underlying; return map keyed by symbol.

        CRITICAL: Use these for mark/bid/ask/delta — never /v2/products prices.
        Products API underlying is BTC; tickers accept BTC (not BTCUSD for options).
        """
        product_underlying = _resolve_product_underlying(underlying)
        params = {
            "underlying_asset_symbols": product_underlying,
            "contract_types": "call_options,put_options",
        }
        result = await self._request(
            "GET",
            "/v2/tickers",
            params=params,
            timeout=CHAIN_TIMEOUT_SECONDS,
        )
        rows: list[Any]
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = list(
                result.get("tickers")
                or result.get("result")
                or []
            )
            if not rows and "symbol" in result:
                rows = [result]
        else:
            rows = []

        ticker_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if symbol:
                ticker_map[str(symbol)] = row

        if not ticker_map:
            # Fallback param shape used by some Delta deployments
            result2 = await self._request(
                "GET",
                "/v2/tickers",
                params={"underlying_asset_symbol": product_underlying},
                timeout=CHAIN_TIMEOUT_SECONDS,
            )
            rows2 = result2 if isinstance(result2, list) else []
            for row in rows2:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("symbol")
                ctype = str(row.get("contract_type") or "").lower()
                if symbol and ctype in {"call_options", "put_options"}:
                    ticker_map[str(symbol)] = row

        logger.info(
            "Fetched %s live option tickers for underlying=%s",
            len(ticker_map),
            product_underlying,
        )
        return ticker_map

    async def get_available_expiries(
        self,
        underlying: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return future option expiries for underlying.

        Default limit is MAX_EXPIRIES_RETURNED (7). Pass a larger limit for
        monthly hedge expiry resolution.
        Each item: { date, label, key, timestamp }
        """
        product_underlying = _resolve_product_underlying(underlying)
        products = await self._get_products("call_options", product_underlying)
        now_ts = int(get_ist_now().timestamp())
        by_date: dict[date, int] = {}

        for product in products:
            exp_date, ts = _expiry_from_product(product)
            if exp_date is None or ts is None:
                continue
            if ts <= now_ts:
                continue
            existing = by_date.get(exp_date)
            if existing is None or ts < existing:
                by_date[exp_date] = ts

        cap = MAX_EXPIRIES_RETURNED if limit is None else max(1, int(limit))
        sorted_dates = sorted(by_date.keys())[:cap]
        date_list = list(sorted_dates)
        return [
            {
                "date": d.isoformat(),
                "label": get_dte_label(d, date_list),
                "key": get_expiry_label_key(d, date_list),
                "timestamp": by_date[d],
            }
            for d in date_list
        ]

    async def get_option_chain(
        self,
        underlying: str,
        expiry_date: str,
    ) -> list[dict[str, Any]]:
        """
        Build unified call+put option chain for underlying and expiry date.

        Two-step fetch:
          1) /v2/products → strike / symbol / product_id metadata only
          2) /v2/tickers  → live mark_price, bid, ask, delta

        NEVER uses product mark prices (stale/incorrect on Delta India).
        Only strikes with BOTH call and put AND live ticker data are included.
        highlight=True when either side mark is within ±$20 of $150 target.
        """
        try:
            target_date = date.fromisoformat(expiry_date)
        except ValueError as exc:
            raise DeltaAPIError(400, f"Invalid expiry_date: {expiry_date}") from exc

        product_underlying = _resolve_product_underlying(underlying)
        calls = await self._get_products("call_options", product_underlying)
        puts = await self._get_products("put_options", product_underlying)
        ticker_map = await self._get_option_tickers_map(product_underlying)
        if not ticker_map:
            raise DeltaAPIError(
                502,
                f"No live option tickers returned for {product_underlying}",
            )

        calls_by_strike = self._index_products_by_strike(calls, target_date)
        puts_by_strike = self._index_products_by_strike(puts, target_date)
        common_strikes = sorted(
            set(calls_by_strike.keys()) & set(puts_by_strike.keys())
        )

        chain: list[dict[str, Any]] = []
        skipped_missing_ticker = 0
        for strike in common_strikes:
            call_p = calls_by_strike[strike]
            put_p = puts_by_strike[strike]
            call_symbol = str(call_p.get("symbol", ""))
            put_symbol = str(put_p.get("symbol", ""))
            call_ticker = ticker_map.get(call_symbol)
            put_ticker = ticker_map.get(put_symbol)
            if call_ticker is None or put_ticker is None:
                skipped_missing_ticker += 1
                continue

            call_bid, call_ask, call_mark, call_delta = _extract_live_quote(call_ticker)
            put_bid, put_ask, put_mark, put_delta = _extract_live_quote(put_ticker)
            if call_mark <= 0 and put_mark <= 0:
                skipped_missing_ticker += 1
                continue

            highlight = (
                abs(call_mark - TARGET_PREMIUM_USD) < PREMIUM_HIGHLIGHT_RANGE_USD
                or abs(put_mark - TARGET_PREMIUM_USD) < PREMIUM_HIGHLIGHT_RANGE_USD
            )
            chain.append(
                {
                    "strike": strike,
                    "call_symbol": call_symbol,
                    "call_product_id": int(call_p.get("id") or 0),
                    "call_bid": call_bid,
                    "call_ask": call_ask,
                    "call_mark_price": call_mark,
                    "call_delta": call_delta,
                    "call_theta": _extract_greek(call_ticker, "theta"),
                    "call_vega": _extract_greek(call_ticker, "vega"),
                    "call_iv": _extract_iv(call_ticker),
                    "put_symbol": put_symbol,
                    "put_product_id": int(put_p.get("id") or 0),
                    "put_bid": put_bid,
                    "put_ask": put_ask,
                    "put_mark_price": put_mark,
                    "put_delta": abs(put_delta),
                    "put_theta": _extract_greek(put_ticker, "theta"),
                    "put_vega": _extract_greek(put_ticker, "vega"),
                    "put_iv": _extract_iv(put_ticker),
                    "highlight": highlight,
                }
            )

        if skipped_missing_ticker:
            logger.warning(
                "Option chain %s %s: skipped %s strikes missing live tickers",
                product_underlying,
                expiry_date,
                skipped_missing_ticker,
            )
        return chain

    def _index_products_by_strike(
        self,
        products: list[dict[str, Any]],
        target_date: date,
    ) -> dict[float, dict[str, Any]]:
        """Index live products for a single expiry date by strike_price."""
        indexed: dict[float, dict[str, Any]] = {}
        for product in products:
            exp_date, _ts = _expiry_from_product(product)
            if exp_date != target_date:
                continue
            strike = _safe_float(product.get("strike_price"), default=-1.0)
            if strike < 0:
                continue
            indexed[strike] = product
        return indexed

    async def find_strike_by_premium(
        self,
        underlying: str,
        expiry_date: str,
        leg_type: str,
        target_premium: float,
        exclude_strike: float | None = None,
        require_farther_otm: bool = False,
    ) -> dict[str, Any]:
        """
        Find a strike whose mark is nearest to target_premium.

        Adjustment path (require_farther_otm=True):
          - CALL: only strikes > exclude_strike (farther OTM / UP)
          - PUT:  only strikes < exclude_strike (farther OTM / DOWN)
          - Pick closest abs(mark − target); tie-break farther OTM
          - Never rolls toward the money; empty directional pool → error

        Legacy path (require_farther_otm=False): keep prior above_offer then
        nearest fallback (used by non-adjustment callers / tests).
        """
        leg = leg_type.lower().strip()
        if leg not in {"call", "put"}:
            raise DeltaAPIError(400, f"Invalid leg_type: {leg_type}")

        target = float(target_premium)
        if target <= 0:
            raise DeltaAPIError(400, f"Invalid target_premium: {target_premium}")

        chain = await self.get_option_chain(underlying, expiry_date)
        if not chain:
            raise DeltaAPIError(
                404,
                f"No option chain for {underlying} expiry {expiry_date}",
            )

        mark_key = "call_mark_price" if leg == "call" else "put_mark_price"
        excl = float(exclude_strike) if exclude_strike is not None else None
        direction = "UP" if leg == "call" else "DOWN"

        pool: list[dict[str, Any]] = []
        candidates_scanned = 0
        for row in chain:
            strike = _safe_float(row.get("strike"), default=-1.0)
            if strike < 0:
                continue
            candidates_scanned += 1
            if excl is not None and abs(strike - excl) < 0.01:
                continue
            if require_farther_otm and excl is not None:
                if leg == "call" and strike <= excl:
                    continue
                if leg == "put" and strike >= excl:
                    continue
            mark = _safe_float(row.get(mark_key))
            if mark <= 0:
                continue
            pool.append(row)

        if not pool:
            raise DeltaAPIError(
                404,
                (
                    f"NO_VALID_STRIKE: no {leg} strike {direction} from "
                    f"exclude={excl} for target={target:.2f}"
                ),
            )

        def _nearest_key(row: dict[str, Any]) -> tuple[float, float]:
            strike = _safe_float(row.get("strike"))
            mark = _safe_float(row.get(mark_key))
            prem_diff = abs(mark - target)
            # Farther OTM wins ties
            otm_rank = -strike if leg == "call" else strike
            return (prem_diff, otm_rank)

        def _at_or_above_key(row: dict[str, Any]) -> tuple[float, float]:
            strike = _safe_float(row.get("strike"))
            mark = _safe_float(row.get(mark_key))
            otm_rank = -strike if leg == "call" else strike
            return (mark, otm_rank)

        if require_farther_otm:
            # Adjustment: closest to target among farther-OTM only
            best = min(pool, key=_nearest_key)
            match_mode = "closest_farther_otm"
        else:
            at_or_above = [
                row
                for row in pool
                if _safe_float(row.get(mark_key)) >= target
            ]
            if at_or_above:
                best = min(at_or_above, key=_at_or_above_key)
                match_mode = "above_offer"
            else:
                best = min(pool, key=_nearest_key)
                match_mode = "fallback_nearest"

        best_mark = _safe_float(best.get(mark_key))
        best_strike = _safe_float(best.get("strike"))
        # Hard directional guard (belt-and-suspenders)
        if require_farther_otm and excl is not None:
            if leg == "call" and best_strike <= excl:
                raise DeltaAPIError(
                    404,
                    (
                        f"NO_VALID_STRIKE: call candidate {best_strike} not UP "
                        f"from {excl} (target={target:.2f})"
                    ),
                )
            if leg == "put" and best_strike >= excl:
                raise DeltaAPIError(
                    404,
                    (
                        f"NO_VALID_STRIKE: put candidate {best_strike} not DOWN "
                        f"from {excl} (target={target:.2f})"
                    ),
                )

        result = dict(best)
        result["_match_method"] = match_mode
        result["_direction"] = direction
        result["_candidates_scanned"] = int(candidates_scanned)
        result["_old_strike"] = excl
        result["_deviation_pct"] = (
            abs(best_mark - target) / target * 100.0 if target > 0 else 0.0
        )
        logger.info(
            "Premium match: %s strike=%s mark=%.2f target=%.2f diff=%.2f "
            "exclude=%s mode=%s direction=%s scanned=%s",
            leg,
            best_strike,
            best_mark,
            target,
            abs(best_mark - target),
            excl,
            match_mode,
            direction,
            candidates_scanned,
        )
        return result

    async def find_atm_straddle(
        self,
        underlying: str,
        expiry_date: str,
        tolerance_pct: float | None = None,
    ) -> dict[str, Any]:
        """
        ATM-anchored short straddle: CALL pinned at ATM, PUT premium-matched
        over the full chain at or below ATM (OTM puts only).
        """
        price_map = {"BTC": "BTCUSD", "ETH": "ETHUSD", "XAU": "XAUUSD"}
        key = underlying.upper().strip()
        price_symbol = price_map.get(
            key, key if key.endswith("USD") else f"{key}USD"
        )

        spot_price = await self.get_underlying_price(price_symbol)
        if not spot_price or spot_price <= 0:
            raise DeltaAPIError(
                502, f"Could not get spot price for {underlying} ({price_symbol})"
            )

        chain = await self.get_option_chain(underlying, expiry_date)
        if not chain:
            raise DeltaAPIError(
                404,
                f"No option chain available for {underlying} {expiry_date}. "
                "Market may be closed or expiry unavailable.",
            )

        logger.info(
            "Finding ATM-anchored straddle: %s spot=%.2f expiry=%s chain_rows=%s",
            underlying,
            spot_price,
            expiry_date,
            len(chain),
        )

        picked = select_atm_anchored_pair(
            chain,
            float(spot_price),
            tolerance_pct=tolerance_pct,
        )
        call_row = picked["call_row"]
        put_row = picked["put_row"]
        best_diff = float(picked["premium_diff_pct"])
        candidates_scanned = int(picked["candidates_scanned"])

        call_strike = _safe_float(call_row.get("strike"))
        put_strike = _safe_float(put_row.get("strike"))
        call_premium = _safe_float(call_row.get("call_mark_price"))
        put_premium = _safe_float(put_row.get("put_mark_price"))

        logger.info(
            "ATM-anchored: CALL %s@%.2f (ATM) + PUT %s@%.2f diff=%.1f%% "
            "scanned=%s spot=%.2f",
            call_strike,
            call_premium,
            put_strike,
            put_premium,
            best_diff,
            candidates_scanned,
            spot_price,
        )

        return {
            "call_strike": call_strike,
            "call_symbol": str(call_row.get("call_symbol") or ""),
            "call_product_id": int(call_row.get("call_product_id") or 0),
            "call_premium": call_premium,
            "call_bid": _safe_float(call_row.get("call_bid")),
            "call_ask": _safe_float(call_row.get("call_ask")),
            "call_delta": _safe_float(call_row.get("call_delta")),
            "put_strike": put_strike,
            "put_symbol": str(put_row.get("put_symbol") or ""),
            "put_product_id": int(put_row.get("put_product_id") or 0),
            "put_premium": put_premium,
            "put_bid": _safe_float(put_row.get("put_bid")),
            "put_ask": _safe_float(put_row.get("put_ask")),
            "put_delta": _safe_float(put_row.get("put_delta")),
            "spot_price": float(spot_price),
            "premium_diff_pct": float(best_diff),
            "expiry_date": expiry_date,
            "underlying": underlying.upper().strip(),
            "trade_type": "straddle",
            # Backward compatibility — primary display strike
            "strike": call_strike,
        }

    async def find_strangle_by_premium(
        self,
        underlying: str,
        expiry_date: str,
        target_premium: float,
    ) -> dict[str, Any]:
        """
        Find OTM Call + OTM Put strikes where each premium is closest
        to target_premium. Call and Put are chosen independently.
        """
        price_map = {"BTC": "BTCUSD", "ETH": "ETHUSD", "XAU": "XAUUSD"}
        key = underlying.upper().strip()
        price_symbol = price_map.get(
            key, key if key.endswith("USD") else f"{key}USD"
        )

        spot = await self.get_underlying_price(price_symbol)
        if not spot or spot <= 0:
            raise DeltaAPIError(
                502, f"Could not get spot price for {underlying} ({price_symbol})"
            )

        chain = await self.get_option_chain(underlying, expiry_date)
        if not chain:
            raise DeltaAPIError(
                404,
                f"No option chain available for {underlying} {expiry_date}. "
                "Market may be closed or expiry unavailable.",
            )

        target = float(target_premium)
        logger.info(
            "Finding strangle: %s spot=%.2f target_premium=%.2f expiry=%s",
            underlying,
            spot,
            target,
            expiry_date,
        )

        otm_calls = [
            row
            for row in chain
            if _safe_float(row.get("strike")) > float(spot)
            and _safe_float(row.get("call_mark_price")) > 0
        ]
        otm_puts = [
            row
            for row in chain
            if _safe_float(row.get("strike")) < float(spot)
            and _safe_float(row.get("put_mark_price")) > 0
        ]

        atm_row = min(
            chain,
            key=lambda r: abs(_safe_float(r.get("strike")) - float(spot)),
        )
        if (
            atm_row not in otm_calls
            and _safe_float(atm_row.get("call_mark_price")) > 0
        ):
            otm_calls.append(atm_row)
        if (
            atm_row not in otm_puts
            and _safe_float(atm_row.get("put_mark_price")) > 0
        ):
            otm_puts.append(atm_row)

        if not otm_calls:
            raise DeltaAPIError(
                404, f"No OTM call strikes found for {underlying}"
            )
        if not otm_puts:
            raise DeltaAPIError(
                404, f"No OTM put strikes found for {underlying}"
            )

        best_call = min(
            otm_calls,
            key=lambda r: abs(
                _safe_float(r.get("call_mark_price")) - target
            ),
        )
        best_put = min(
            otm_puts,
            key=lambda r: abs(
                _safe_float(r.get("put_mark_price")) - target
            ),
        )

        call_prem = _safe_float(best_call.get("call_mark_price"))
        put_prem = _safe_float(best_put.get("put_mark_price"))
        call_diff = abs(call_prem - target)
        put_diff = abs(put_prem - target)
        call_strike = _safe_float(best_call.get("strike"))
        put_strike = _safe_float(best_put.get("strike"))

        logger.info(
            "Strangle found: CALL %s @ %.2f (diff $%.2f from target $%.2f) | "
            "PUT %s @ %.2f (diff $%.2f from target $%.2f) | spot=%.2f",
            call_strike,
            call_prem,
            call_diff,
            target,
            put_strike,
            put_prem,
            put_diff,
            target,
            spot,
        )

        return {
            "call_strike": call_strike,
            "call_symbol": str(best_call.get("call_symbol") or ""),
            "call_product_id": int(best_call.get("call_product_id") or 0),
            "call_premium": call_prem,
            "call_bid": _safe_float(best_call.get("call_bid")),
            "call_ask": _safe_float(best_call.get("call_ask")),
            "call_delta": _safe_float(best_call.get("call_delta")),
            "put_strike": put_strike,
            "put_symbol": str(best_put.get("put_symbol") or ""),
            "put_product_id": int(best_put.get("put_product_id") or 0),
            "put_premium": put_prem,
            "put_bid": _safe_float(best_put.get("put_bid")),
            "put_ask": _safe_float(best_put.get("put_ask")),
            "put_delta": _safe_float(best_put.get("put_delta")),
            "spot_price": float(spot),
            "target_premium": target,
            "call_premium_diff": round(call_diff, 2),
            "put_premium_diff": round(put_diff, 2),
            "expiry_date": expiry_date,
            "underlying": underlying.upper().strip(),
            "trade_type": "strangle",
            "strike": call_strike,
        }

    async def close_position(
        self,
        product_id: int,
        size: int,
        is_long: bool,
    ) -> dict[str, Any]:
        """
        Close any position safely with reduce_only=True.
        is_long=True  → SELL to close long
        is_long=False → BUY to close short
        """
        return await self.place_order(
            product_id=product_id,
            size=abs(size),
            side="sell" if is_long else "buy",
            order_type="market_order",
            time_in_force="ioc",
            reduce_only=True,
        )

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self.client.aclose()


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")

    async def _live_auth_check() -> None:
        api_key = os.getenv("DELTA_API_KEY", "").strip()
        api_secret = os.getenv("DELTA_API_SECRET", "").strip()
        if not api_key or not api_secret:
            print("⚠️  DELTA_API_KEY / DELTA_API_SECRET not in .env — skipping live auth")
            print("   Import check only. Live test_connection + get_wallet_balance in TASK-2.3.")
            print("✅ IMPORT OK (live auth deferred — no keys)")
            return

        client = DeltaClient(api_key, api_secret)
        try:
            profile = await client.test_connection()
            print(f"Profile: {profile}")
            wallet = await client.get_wallet_balance()
            print(f"Wallet: {wallet}")
            print("✅ LIVE AUTH + WALLET OK")
        finally:
            await client.close()

    asyncio.run(_live_auth_check())
