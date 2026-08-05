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
from backend.core.time_utils import get_dte_label, get_ist_now

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
    Signed PnL in USD: (exit_ref - entry) * size * contract_value
    where size is signed (negative = short).

    For short options matching Delta UI "UPL @offer", pass best_ask as mark_price
    (closing a short = buy at offer). Mid/mark alone will diverge from Delta portfolio.
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

    @staticmethod
    def _extract_unrealized_pnl(pos: dict[str, Any]) -> float:
        """Compute signed UPNL — never trust API unrealized_pnl for short options."""
        return compute_signed_upnl(
            entry_price=_safe_float(pos.get("entry_price")),
            mark_price=_safe_float(pos.get("mark_price")),
            size=_safe_float(pos.get("size")),
            contract_value=_contract_value_from_position(pos),
        )

    async def get_positions_upnl(
        self, product_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """
        Per-leg UPNL matching Delta UI "UPL @offer".

        IMPORTANT: Do NOT use API field `unrealized_pnl` for short options —
        on Delta India it returns mark * |size| * contract_value (premium
        notional), NOT signed UPL. Example: mark 460, size -10, cv 0.001 →
        api shows ~4.6 while real UPL@offer is (entry-offer)*|size|*cv.

        Correct formula (shorts): (best_offer - entry) * size * cv
        with size < 0, identical to (entry - best_offer) * |size| * cv.
        Best offer = L2 top ask (same book Delta UI uses).
        """
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
            if pid not in wanted:
                continue

            entry = _safe_float(pos.get("entry_price"))
            mark = _safe_float(pos.get("mark_price"))
            size = _safe_float(pos.get("size"))
            cv = _contract_value_from_position(pos)
            symbol = str(pos.get("symbol") or product.get("symbol") or "")
            api_raw = _safe_float(pos.get("unrealized_pnl"))

            logger.info(
                "Position %s fields_pnl=%s api_unrealized_pnl=%s "
                "(IGNORED — not UPL; ~mark*|size|*cv) entry=%s mark=%s size=%s",
                pid,
                [k for k in pos.keys() if "pnl" in k.lower() or "upl" in k.lower()],
                api_raw,
                entry,
                mark,
                size,
            )

            best_offer = 0.0
            if symbol and size < 0:
                try:
                    best_offer = float(await self.get_short_exit_price(symbol))
                except Exception as exc:
                    logger.warning(
                        "Best offer fetch failed pid=%s symbol=%s: %s",
                        pid,
                        symbol,
                        exc,
                    )
            elif symbol and size > 0:
                try:
                    best_offer = float(await self.get_long_exit_price(symbol))
                except Exception as exc:
                    logger.warning(
                        "Best bid fetch failed pid=%s symbol=%s: %s",
                        pid,
                        symbol,
                        exc,
                    )

            exit_ref = best_offer if best_offer > 0 else mark
            upnl = compute_signed_upnl(entry, exit_ref, size, cv)

            logger.info(
                "Position %s UPL@offer=%.6f entry=%.4f offer=%.4f size=%s cv=%s "
                "symbol=%s (api_raw=%.6f ignored)",
                pid,
                upnl,
                entry,
                exit_ref,
                size,
                cv,
                symbol,
                api_raw,
            )

            out[pid] = {
                "upnl": float(upnl),
                "entry_price": entry,
                "mark_price": mark,
                "best_offer": float(best_offer) if best_offer > 0 else float(exit_ref),
                "size": size,
                "symbol": symbol,
                "contract_value": cv,
                "api_unrealized_pnl_raw": api_raw,
            }

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
        """Convenience: product_id → UPL@offer USD (matches Delta UI)."""
        detailed = await self.get_positions_upnl(product_ids)
        return {pid: float(row["upnl"]) for pid, row in detailed.items()}

    async def place_order(
        self,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        time_in_force: str = "ioc",
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
        POST /v2/orders — stop-loss that triggers a market buy-to-close.

        Delta India format (not stop_market_order):
          order_type=market_order
          stop_order_type=stop_loss_order
          stop_price (string)
          reduce_only="true"
          time_in_force=gtc
        """
        body: dict[str, Any] = {
            "product_id": int(product_id),
            "size": abs(int(size)),
            "side": side,
            "order_type": "market_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": str(round(float(stop_price), 2)),
            "time_in_force": "gtc",
            "reduce_only": "true",
        }
        logger.info(
            "Placing stop order: product=%s side=%s stop_price=%s body=%s",
            product_id,
            side,
            stop_price,
            body,
        )
        result = await self._request(
            "POST",
            "/v2/orders",
            body=body,
            timeout=ORDER_TIMEOUT_SECONDS,
        )
        logger.info("Stop order placed: %s", result)
        raw = result if isinstance(result, dict) else {"result": result}
        return {
            **raw,
            "order_id": raw.get("id") or raw.get("order_id"),
            "status": raw.get("state") or raw.get("status"),
            "stop_price": float(stop_price),
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

    async def get_available_expiries(self, underlying: str) -> list[dict[str, Any]]:
        """
        Return future option expiries for underlying (max 7 nearest).

        Each item: { date: "YYYY-MM-DD", label: "1DTE", timestamp: unix_ts }
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

        sorted_dates = sorted(by_date.keys())[:MAX_EXPIRIES_RETURNED]
        return [
            {
                "date": d.isoformat(),
                "label": get_dte_label(d),
                "timestamp": by_date[d],
            }
            for d in sorted_dates
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
                    "put_symbol": put_symbol,
                    "put_product_id": int(put_p.get("id") or 0),
                    "put_bid": put_bid,
                    "put_ask": put_ask,
                    "put_mark_price": put_mark,
                    "put_delta": abs(put_delta),
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
        CORE ADJUSTMENT RULE (mandatory once trigger fires):

        Exit triggered leg and sell the strike whose mark is NEAREST to the
        other open leg's premium (e.g. put@$110 → new call ≈ $110).

        - Exclude current strike (same-strike adjust = fees only)
        - Primary sort: abs(mark - target_premium)
        - Tie-break: prefer farther OTM (call higher / put lower)
        - Always returns a strike if any other exists — never HOLD for imperfect match
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

        pool: list[dict[str, Any]] = []
        for row in chain:
            strike = _safe_float(row.get("strike"), default=-1.0)
            if strike < 0:
                continue
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
                    f"SAME_STRIKE_HOLD: no other {leg} strike on chain "
                    f"(exclude={excl}, target={target:.2f})"
                ),
            )

        def _sort_key(row: dict[str, Any]) -> tuple[float, float]:
            strike = _safe_float(row.get("strike"))
            mark = _safe_float(row.get(mark_key))
            prem_diff = abs(mark - target)
            # Farther OTM wins ties
            otm_rank = -strike if leg == "call" else strike
            return (prem_diff, otm_rank)

        best = min(pool, key=_sort_key)
        best_mark = _safe_float(best.get(mark_key))
        best_strike = _safe_float(best.get("strike"))
        logger.info(
            "Premium match (mandatory adjust): %s strike=%s mark=%.2f "
            "target=%.2f diff=%.2f exclude=%s",
            leg,
            best_strike,
            best_mark,
            target,
            abs(best_mark - target),
            excl,
        )
        return best

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
