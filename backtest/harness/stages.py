"""KILL / DESIGN / CONFIRM stage runners + run artifact writers."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backtest.harness.config import RUNS_DIR, HarnessConfig, default_config
from backtest.harness.engine import HarnessEngine
from backtest.harness.models import CycleResult

logger = logging.getLogger("harness.stages")


def _stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_run_artifacts(
    *,
    strategy_id: str,
    stage: str,
    cfg: HarnessConfig,
    stats: dict[str, Any],
    cycles: list[CycleResult],
    verdict: str,
) -> tuple[Path, Path]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    base = f"{strategy_id}_{stage}_{stamp}"
    json_path = RUNS_DIR / f"{base}.json"
    md_path = RUNS_DIR / f"{base}.md"

    payload = {
        "strategy_id": strategy_id,
        "stage": stage,
        "verdict": verdict,
        "window": {
            "from": cfg.from_date.isoformat(),
            "to": cfg.to_date.isoformat(),
            "tag": cfg.window_tag,
        },
        "config": {
            "slip_mult": cfg.slip_mult,
            "slip_model": cfg.slip_model,
            "bootstrap_n": cfg.bootstrap_n,
            "bootstrap_seed": cfg.bootstrap_seed,
            "strategy_params": cfg.strategy_params,
        },
        "metrics": stats,
        "n_cycles_written": len(cycles),
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    md_lines = [
        f"# {strategy_id} — {stage}",
        "",
        f"**Verdict:** {verdict}",
        f"**Window:** {cfg.from_date} → {cfg.to_date} ({cfg.window_tag})",
        f"**Generated:** {payload['generated_utc']}",
        "",
        "## Metrics",
        f"- n_cycles: {stats.get('n_cycles')}",
        f"- win%: {stats.get('win_pct')}",
        f"- mean/day: {stats.get('mean_day')}",
        f"- CI: [{stats.get('ci_lo')}, {stats.get('ci_hi')}]",
        f"- worst: {stats.get('worst_net')} on {stats.get('worst_date')}",
        f"- max DD: {stats.get('max_dd')}",
        f"- exit mix: {stats.get('exit_mix')}",
        "",
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    logger.info("wrote %s and %s", json_path, md_path)
    return json_path, md_path


def auto_verdict(stage: str, stats: dict[str, Any]) -> str:
    n = int(stats.get("n_cycles") or 0)
    mean_day = stats.get("mean_day")
    ci_lo = stats.get("ci_lo")
    if n <= 0:
        return "NO_DATA"
    if stage == "KILL":
        # soft gate: not catastrophic
        if mean_day is not None and mean_day == mean_day and mean_day < -1.0:
            return "KILL_FAIL"
        return "KILL_PASS"
    if stage == "CONFIRM":
        if ci_lo is not None and ci_lo == ci_lo and ci_lo > 0:
            return "CONFIRM_PASS"
        return "CONFIRM_FAIL"
    # DESIGN
    return "DESIGN_COMPLETE"


def run_stage(
    strategy: Any,
    stage: str,
    *,
    cfg: HarnessConfig | None = None,
    write: bool = True,
) -> dict[str, Any]:
    stage_u = stage.upper()
    run_cfg = cfg or default_config(stage_u)
    run_cfg.stage = stage_u
    engine = HarnessEngine(strategy, run_cfg)
    cycles, skips, stats = engine.run()
    verdict = auto_verdict(stage_u, stats)
    out: dict[str, Any] = {
        "stats": stats,
        "verdict": verdict,
        "n_cycles": len(cycles),
        "skips": skips,
    }
    if write:
        meta = strategy.meta()
        jp, mp = write_run_artifacts(
            strategy_id=meta.id,
            stage=stage_u,
            cfg=run_cfg,
            stats=stats,
            cycles=cycles,
            verdict=verdict,
        )
        out["json_path"] = str(jp)
        out["md_path"] = str(mp)
    return out
