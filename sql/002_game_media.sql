-- Migration 002: give games a description and a picture.
-- Run this in the Neon SQL Editor. Safe to re-run.
--
-- The image lives in Postgres as bytea rather than in a file, because Vercel's
-- filesystem is read-only and discarded between requests — there is nowhere to
-- write an upload to. Storing it in the database means no second service and no
-- second set of credentials. Uploads are resized to 800px on the way in, so a
-- typical image is around 100KB.

alter table games add column if not exists description text;
alter table games add column if not exists image       bytea;
alter table games add column if not exists image_type  text;

-- Cheap way for the list page to know whether a game has a picture without
-- pulling the bytes back over the wire.
create or replace view games_summary as
select
    id, name, mode, min_players, max_players, active, description, created_at,
    (image is not null) as has_image
from games;