-- Flag structures whose attribution may be incomplete (open leg windows
-- forced closed at structure close, or closed_at was missing on exit).
-- Earner should treat non-NULL attribution_warning as SUSPECT.
--
-- Run manually (SQLite):
--   sqlite3 path/to/trading_bot.db < backend/migrations/002_add_structure_attribution_warning.sql
--
-- Or rely on automatic migration in database._migrate_schema() on next app start.

ALTER TABLE structures ADD COLUMN attribution_warning TEXT;
