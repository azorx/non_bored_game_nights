-- Migration 007: per-session celebratory awards (the non-game titles).
-- Run this in the Neon SQL Editor. Safe to re-run.
--
-- These have nothing to do with who won a game — they are the fun stuff voted on
-- at the table each night. The first is 'best_snack', which powers the
-- "Professional Snacker" title (whoever's snack was voted best on the most
-- nights). The `award` column is deliberately free text so a new celebratory
-- title is just a new award name plus a bit of admin UI — no schema change.
--
-- One winner per award per night: the primary key (session_id, award) means
-- re-recording simply overwrites, it never stacks duplicates.

create table if not exists session_awards (
    session_id uuid        not null references sessions (id) on delete cascade,
    award      text        not null,
    player_id  uuid        not null references players (id)  on delete cascade,
    created_at timestamptz not null default now(),
    primary key (session_id, award)
);
