# conftest.py — pytest collection helpers for trading-bot tests

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_api: requires DELTA_API_KEY / DELTA_API_SECRET in the environment",
    )
    config.addinivalue_line(
        "markers",
        "live_ws: requires a running local backend (ws://127.0.0.1:8000)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    has_delta = bool(
        os.getenv("DELTA_API_KEY", "").strip()
        and os.getenv("DELTA_API_SECRET", "").strip()
    )
    run_live_ws = os.getenv("RUN_LIVE_WS", "").strip() == "1"

    skip_api = pytest.mark.skip(
        reason="DELTA_API_KEY / DELTA_API_SECRET not set (live API test)"
    )
    skip_ws = pytest.mark.skip(
        reason="Set RUN_LIVE_WS=1 with backend up (live websocket test)"
    )

    for item in items:
        path = str(item.fspath)
        name = item.name
        # Live Delta REST integration tests
        if "test_integration.py" in path and name in {
            "test_1_api_connection",
            "test_2_option_chain",
            "test_3_find_strike_by_premium",
        }:
            if not has_delta:
                item.add_marker(skip_api)
        # Manual websocket smoke scripts collected as tests
        if "test_chain_ws.py" in path or "test_websocket.py" in path:
            if not run_live_ws:
                item.add_marker(skip_ws)
