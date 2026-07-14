-- Migration 003: how long a game typically takes.
-- Run this in the Neon SQL Editor. Safe to re-run.
--
-- Nullable on purpose: "I don't know how long Codenames takes" is a legitimate
-- state, and forcing a made-up number would be worse than showing nothing.

alter table games add column if not exists duration_minutes int
    check (duration_minutes is null or duration_minutes between 1 and 600);

-- NOTE: `create or replace view` can only APPEND columns to an existing view —
-- it cannot reorder or rename them. Since duration_minutes belongs before
-- created_at, the view has to be dropped and rebuilt. Nothing depends on it but
-- the app's read queries, so this is free.
drop view if exists games_summary;

create view games_summary as
select
    id, name, mode, min_players, max_players, active, description,
    duration_minutes, created_at,
    (image is not null) as has_image
from games;

-- Reasonable starting guesses for the seeded games. Adjust to your group —
-- your Catan games are probably longer than the box claims.
update games set duration_minutes = 75  where lower(name) = 'catan'           and duration_minutes is null;
update games set duration_minutes = 15  where lower(name) = 'codenames'       and duration_minutes is null;
update games set duration_minutes = 30  where lower(name) = 'chess'           and duration_minutes is null;
update games set duration_minutes = 60  where lower(name) = 'wingspan'        and duration_minutes is null;
update games set duration_minutes = 45  where lower(name) = 'ticket to ride'  and duration_minutes is null;
update games set duration_minutes = 15  where lower(name) = 'coup'            and duration_minutes is null;
update games set duration_minutes = 35  where lower(name) = 'azul'            and duration_minutes is null;