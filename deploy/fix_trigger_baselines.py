#!/usr/bin/env python3
"""
One-shot: backfill trigger_baseline_premium + fix Trade #2 entry/baseline split.

Run on server after deploy:
  cd /home/botuser/trading-bot
  /home/botuser/.venv/bin/python deploy/fix_trigger_baselines.py
  sudo supervisorctl restart trading-bot
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.database import SessionLocal, init_db
from backend.models import Leg, Trade


# Known broken state after initial_premium was overwritten on untouched PUT
TRADE_2_FIX = {
    "put": {"initial_premium": 400.0, "trigger_baseline_premium": 69.0},
    "call": {"initial_premium": 43.0, "trigger_baseline_premium": 43.0},
}


def main() -> None:
    init_db()
    with SessionLocal() as db:
        # Backfill any NULL baselines
        legs = db.query(Leg).all()
        filled = 0
        for leg in legs:
            if (
                getattr(leg, "trigger_baseline_premium", None) is None
                or float(leg.trigger_baseline_premium or 0) <= 0
            ):
                base = float(
                    getattr(leg, "trigger_premium", None)
                    or leg.initial_premium
                    or 0
                )
                leg.trigger_baseline_premium = base
                if getattr(leg, "trigger_premium", None) is None:
                    leg.trigger_premium = base
                filled += 1
        print(f"Backfilled trigger_baseline_premium on {filled} leg(s)")

        trade = db.query(Trade).filter(Trade.id == 2).first()
        if trade is None:
            print("Trade #2 not found — skip hardcoded restore")
        else:
            open_legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == 2,
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            for leg in open_legs:
                key = str(leg.leg_type).lower()
                fix = TRADE_2_FIX.get(key)
                if not fix:
                    continue
                print(
                    f"Trade #2 {key}: entry "
                    f"{leg.initial_premium} → {fix['initial_premium']}, "
                    f"baseline {getattr(leg, 'trigger_baseline_premium', None)} "
                    f"→ {fix['trigger_baseline_premium']}"
                )
                leg.initial_premium = float(fix["initial_premium"])
                leg.trigger_baseline_premium = float(
                    fix["trigger_baseline_premium"]
                )
                leg.trigger_premium = float(fix["trigger_baseline_premium"])

        db.commit()
        print("Done. Restart trading-bot to reload tracker.")


if __name__ == "__main__":
    main()
