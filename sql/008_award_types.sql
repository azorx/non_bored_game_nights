-- Migration 008: a persistent registry of celebratory awards.
-- Run this in the Neon SQL Editor AFTER 007. Safe to re-run.
--
-- Until now an "award type" was implicit — it existed only for as long as some
-- night had a winner recorded against that name. The admin wants awards to be
-- first-class and persistent: added and removed on their own, independent of any
-- game night, and always shown on the leaderboard even before anyone has won
-- one. So the list of awards lives in its own table.
--
-- session_awards.award now points at that registry, with ON DELETE CASCADE:
-- removing an award from the registry takes its recorded winners with it, so the
-- leaderboard and the session cards forget it cleanly.

create table if not exists award_types (
    name       text        primary key,
    created_at timestamptz not null default now()
);

-- Keep anything already handed out: every award name in use becomes a
-- registered type, so no history is orphaned by the new foreign key.
insert into award_types (name)
select distinct award from session_awards
on conflict do nothing;

alter table session_awards
    drop constraint if exists session_awards_award_fkey,
    add constraint session_awards_award_fkey
        foreign key (award) references award_types (name)
        on update cascade on delete cascade;
