-- Migration 006: a fourth game mode — cooperative.
-- Run this in the Neon SQL Editor. Safe to re-run.
--
-- Co-op games are won or lost by the whole table together; nobody finishes
-- ahead of anyone else, so they carry no Elo. Their leaderboard is a win-rate
-- board instead, read straight from the matches. All that changes at the schema
-- level is that 'coop' becomes a legal value for games.mode.
--
-- The original CHECK was created inline with the table, so Postgres named it
-- games_mode_check. Drop it and re-add it with the extra value. Existing rows
-- are unaffected — they are all still one of the first three modes.

alter table games drop constraint if exists games_mode_check;

alter table games
    add constraint games_mode_check check (mode in ('ffa', 'duel', 'team', 'coop'));
