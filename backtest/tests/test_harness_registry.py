"""Unit tests for harness registry builder."""

from __future__ import annotations

from backtest.harness.registry import build_registry, write_registry
from backtest.strategies.s001_wing_capped_strangle.strategy import (
    REGRESSION_MEAN_DAY,
    REGRESSION_N,
    check_regression,
)


def test_build_registry_has_core_ids() -> None:
    reg = build_registry()
    ids = {s["id"] for s in reg["strategies"]}
    assert {"S001", "S002", "S003", "S004"} <= ids
    s001 = next(s for s in reg["strategies"] if s["id"] == "S001")
    assert s001["status"] == "CLOSED"
    assert s001["name"]


def test_write_registry(tmp_path, monkeypatch) -> None:
    import backtest.harness.registry as regmod

    out = tmp_path / "registry.json"
    monkeypatch.setattr(regmod, "REGISTRY_JSON", out)
    path = write_registry()
    assert path.exists()
    assert "S001" in path.read_text(encoding="utf-8")


def test_s001_regression_checker_ok() -> None:
    ok, msg = check_regression(
        {"n_cycles": REGRESSION_N, "mean_day": REGRESSION_MEAN_DAY}
    )
    assert ok
    assert "REGRESSION OK" in msg


def test_s001_regression_checker_fail_is_loud() -> None:
    ok, msg = check_regression({"n_cycles": 1, "mean_day": 0.0})
    assert not ok
    assert msg.startswith("REGRESSION FAIL")
