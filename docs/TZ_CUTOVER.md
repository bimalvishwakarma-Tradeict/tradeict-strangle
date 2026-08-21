# Timezone cutover

`TZ_CUTOVER_UTC=2026-08-21T18:07:49+00:00` (phase-1 UTC writers, commit `b52d738`) — timestamps before this may be mixed IST/UTC and must not be trusted for duration or cross-table ordering.
