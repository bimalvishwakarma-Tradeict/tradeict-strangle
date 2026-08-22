-- Add earner attribution window override to structure_legs.
-- NULL = earner uses opened_at as window start (default for new legs after e3e6b7d).
-- Set explicitly on pre-fix legs where opened_at post-dates the entry fill.
--
-- Run manually (SQLite):
--   sqlite3 path/to/trading_bot.db < backend/migrations/001_add_structure_leg_attribution_from.sql
--
-- Or rely on automatic migration in database._migrate_schema() on next app start.

ALTER TABLE structure_legs ADD COLUMN attribution_from DATETIME;
