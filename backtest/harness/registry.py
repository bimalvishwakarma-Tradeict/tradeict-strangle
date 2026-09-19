"""Scan strategy folders + runs → registry.json (+ LEARNINGS.md append)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backtest.harness.config import (
    LEARNINGS_MD,
    REGISTRY_JSON,
    RUNS_DIR,
    STRATEGIES_DIR,
)

logger = logging.getLogger("harness.registry")


def _read_spec(spec_path: Path) -> dict[str, str]:
    text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    one_line = ""
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            one_line = s[:200]
            break
    rules = ""
    m = re.search(r"(?is)##\s*rules\s*\n(.+?)(?=\n##|\Z)", text)
    if m:
        rules = m.group(1).strip()[:800]
    return {"one_line": one_line, "rules_summary": rules or text[:400], "raw": text}


def _parse_runs(strategy_id: str) -> list[dict[str, Any]]:
    if not RUNS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(RUNS_DIR.glob(f"{strategy_id}_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip run %s: %s", p, e)
            continue
        metrics = data.get("metrics") or {}
        window = data.get("window") or {}
        out.append(
            {
                "stage": data.get("stage"),
                "window": f"{window.get('from')}..{window.get('to')}",
                "window_tag": window.get("tag"),
                "date": data.get("generated_utc", "")[:10],
                "n": metrics.get("n_cycles"),
                "win_pct": metrics.get("win_pct"),
                "mean_day": metrics.get("mean_day"),
                "ci_lo": metrics.get("ci_lo"),
                "ci_hi": metrics.get("ci_hi"),
                "worst_net": metrics.get("worst_net"),
                "max_dd": metrics.get("max_dd"),
                "verdict": data.get("verdict"),
                "path": str(p.relative_to(p.parents[2]) if len(p.parts) > 2 else p),
            }
        )
    return out


def _load_sidecar_meta(folder: Path) -> dict[str, Any]:
    meta_path = folder / "registry_meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def build_registry() -> dict[str, Any]:
    strategies: list[dict[str, Any]] = []
    if STRATEGIES_DIR.exists():
        for folder in sorted(STRATEGIES_DIR.iterdir()):
            if not folder.is_dir() or folder.name.startswith("_"):
                continue
            spec = _read_spec(folder / "spec.md")
            side = _load_sidecar_meta(folder)
            sid = str(side.get("id") or folder.name)
            strategies.append(
                {
                    "id": sid,
                    "name": side.get("name") or folder.name.replace("_", " ").title(),
                    "status": side.get("status") or "TESTING",
                    "one_line": side.get("one_line") or spec["one_line"],
                    "rules_summary": side.get("rules_summary") or spec["rules_summary"],
                    "tests_done": side.get("tests_done") or _parse_runs(sid),
                    "open_questions": side.get("open_questions") or [],
                    "next_steps": side.get("next_steps") or [],
                    "learnings": side.get("learnings") or [],
                    "folder": folder.name,
                }
            )

    learnings: list[str] = []
    for s in strategies:
        for L in s.get("learnings") or []:
            learnings.append(f"[{s['id']}] {L}")

    registry = {
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
        "strategies": strategies,
        "learnings": learnings,
    }
    return registry


def write_registry(registry: dict[str, Any] | None = None) -> Path:
    reg = registry or build_registry()
    REGISTRY_JSON.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_JSON.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    logger.info("wrote %s (%d strategies)", REGISTRY_JSON, len(reg.get("strategies") or []))
    return REGISTRY_JSON


def append_learnings(lines: list[str]) -> None:
    if not lines:
        return
    LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    block = [f"\n## {stamp}\n"] + [f"- {ln}\n" for ln in lines]
    with LEARNINGS_MD.open("a", encoding="utf-8") as f:
        f.writelines(block)
    logger.info("appended %d learnings to %s", len(lines), LEARNINGS_MD)


def load_registry() -> dict[str, Any]:
    if not REGISTRY_JSON.exists():
        return build_registry()
    return json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    write_registry()
