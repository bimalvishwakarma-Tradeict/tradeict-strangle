#!/usr/bin/env python3
"""
Manual repair: recompute realized_pnl for trades whose legs were booked with
exit_premium=0.0 (false free buyback) while EXIT_CLOSE logs / known fills exist.

Does NOT auto-run. Invoke explicitly after deploy:

  cd /path/to/trading-bot
  python deploy/repair_zero_exit_premiums.py --dry-run
  python deploy/repair_zero_exit_premiums.py --apply

Optional: --trade-id 66 to limit to one trade.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.delta_client import short_leg_realized_pnl
from backend.database import SessionLocal, init_db
from backend.engine.trade_reconcile import recompute_trade_realized_pnl
from backend.models import Leg, Trade

# Parse bot_activity lines like:
# EXIT_CLOSE ... leg=put ... fill=3.0
_FILL_RE = re.compile(
    r"Trade#(?P<tid>\d+).*leg=(?P<leg>call|put|hedge_call|hedge_put)"
    r".*fill=(?P<fill>-?[0-9.]+)",
    re.IGNORECASE,
)


def _load_fills_from_logs(log_path: Path) -> dict[tuple[int, str], float]:
    """Map (trade_id, leg_type) -> last EXIT_CLOSE fill seen in logs."""
    fills: dict[tuple[int, str], float] = {}
    if not log_path.is_file():
        print(f"No log file at {log_path} — skipping log-based fills")
        return fills
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "EXIT_CLOSE" not in line or "fill=" not in line:
            continue
        m = _FILL_RE.search(line)
        if not m:
            continue
        tid = int(m.group("tid"))
        leg = m.group("leg").lower()
        fill = float(m.group("fill"))
        if fill > 0:
            fills[(tid, leg)] = fill
    return fills


def _recompute_leg_realized(leg: Leg, exit_px: float) -> float:
    entry = float(leg.initial_premium or 0.0)
    qty = abs(int(leg.quantity or 0))
    if bool(getattr(leg, "is_long", False)):
        from backend.config import OPTIONS_CONTRACT_VALUE

        return (exit_px - entry) * qty * float(OPTIONS_CONTRACT_VALUE)
    return short_leg_realized_pnl(entry, exit_px, qty)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", help="Write changes")
    parser.add_argument("--trade-id", type=int, default=None)
    parser.add_argument(
        "--log",
        type=Path,
        default=_ROOT / "logs" / "bot_activity.log",
    )
    args = parser.parse_args()
    do_apply = bool(args.apply)
    if do_apply:
        args.dry_run = False

    init_db()
    fills = _load_fills_from_logs(args.log)
    print(f"Loaded {len(fills)} EXIT_CLOSE fills from {args.log}")

    with SessionLocal() as db:
        q = db.query(Leg).filter(Leg.exit_premium == 0.0)
        if args.trade_id is not None:
            q = q.filter(Leg.trade_id == int(args.trade_id))
        bad_legs = q.all()
        print(f"Found {len(bad_legs)} legs with exit_premium=0.0")

        touched_trades: set[int] = set()
        for leg in bad_legs:
            tid = int(leg.trade_id)
            lt = str(leg.leg_type or "").lower()
            fill = fills.get((tid, lt))
            if fill is None or fill <= 0:
                print(
                    f"  SKIP leg_id={leg.id} trade={tid} {lt} — no log fill"
                )
                continue
            new_realized = _recompute_leg_realized(leg, fill)
            print(
                f"  FIX leg_id={leg.id} trade={tid} {lt}: "
                f"exit 0.0 -> {fill:.4f} realized -> {new_realized:.6f}"
            )
            if do_apply:
                leg.exit_premium = float(fill)
                leg.realized_pnl = float(new_realized)
            touched_trades.add(tid)

        for tid in sorted(touched_trades):
            trade = db.query(Trade).filter(Trade.id == tid).first()
            if trade is None:
                continue
            if do_apply:
                total = recompute_trade_realized_pnl(db, trade)
            else:
                # Preview only
                legs = (
                    db.query(Leg)
                    .filter(Leg.trade_id == tid, Leg.is_bot_managed.is_(True))
                    .all()
                )
                total = 0.0
                for leg in legs:
                    if str(leg.status).lower() != "closed":
                        continue
                    lt = str(leg.leg_type or "").lower()
                    if float(leg.exit_premium or 0) == 0.0 and (tid, lt) in fills:
                        total += _recompute_leg_realized(leg, fills[(tid, lt)])
                    elif leg.realized_pnl is not None:
                        total += float(leg.realized_pnl)
            print(
                f"  Trade {tid}: realized_pnl "
                f"{getattr(trade, 'realized_pnl', None)} -> {total:.6f}"
            )

        if do_apply:
            db.commit()
            print("Applied and committed.")
        else:
            print("Dry-run only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
