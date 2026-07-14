-- ===========================================================================
-- Non-Bored Game Nights — COMPLETE schema, all in one file.
-- ===========================================================================
--
-- This is the whole database as it stands today. Running this one file on a
-- fresh Neon database is equivalent to running, in order:
--     schema.sql, 002_game_media.sql, 003_game_duration.sql,
--     004_player_emojis.sql (data only), 005_session_seats.sql
-- but without the historical back-and-forth — every table and column is defined
-- once, in its final form.
--
-- Use this for a brand-new database. Everything is IF NOT EXISTS / OR REPLACE,
-- so it is safe to re-run: it will not clobber data. (To empty an existing
-- database, that is reset.sql, not this file.)
--
-- Run it in the Neon SQL Editor (Console -> your project -> SQL Editor).

-- ---------------------------------------------------------------------------
-- People
-- ---------------------------------------------------------------------------

create table if not exists players (
    id          uuid        primary key default gen_random_uuid(),
    name        text        not null,
    emoji       text        not null default '🎲',
    created_at  timestamptz not null default now()
);

-- Case-insensitive uniqueness: "Dave" and "dave" are the same person, and this
-- is what lets sign-up safely find-or-create by typed name.
create unique index if not exists players_name_lower_idx
    on players (lower(name));


-- ---------------------------------------------------------------------------
-- Games
-- ---------------------------------------------------------------------------

create table if not exists games (
    id               uuid        primary key default gen_random_uuid(),
    name             text        not null,
    -- The mode drives the leaderboard maths (see elo.py):
    --   'ffa'  = free-for-all, everyone finishes in a position
    --   'duel' = strictly two sides (chess, backgammon)
    --   'team' = teams of players finish in a position
    --   'coop' = the whole table wins or loses together (no Elo; win-rate board)
    mode             text        not null default 'ffa'
                                 check (mode in ('ffa', 'duel', 'team', 'coop')),
    min_players      int         not null default 2 check (min_players >= 2),
    max_players      int         not null default 8,
    active           boolean     not null default true,
    description      text,
    image            bytea,      -- stored in Postgres: Vercel's disk is read-only
    image_type       text,
    duration_minutes int         check (duration_minutes is null
                                        or duration_minutes between 1 and 600),
    created_at       timestamptz not null default now(),
    check (max_players >= min_players)
);

create unique index if not exists games_name_lower_idx
    on games (lower(name));

-- Cheap way for list pages to know whether a game has a picture without pulling
-- the image bytes back over the wire. Columns are in their final order here, so
-- unlike the migration path there is no drop-and-rebuild dance.
create or replace view games_summary as
select
    id, name, mode, min_players, max_players, active, description,
    duration_minutes, created_at,
    (image is not null) as has_image
from games;


-- ---------------------------------------------------------------------------
-- Sessions (one game night)
-- ---------------------------------------------------------------------------

create table if not exists sessions (
    id            uuid        primary key default gen_random_uuid(),
    -- Short, guessable-but-not-guessed token used in the public URL, so the
    -- WhatsApp link is /s/x7k2p9qa rather than a 36-character UUID.
    slug          text        not null unique,
    title         text        not null,
    scheduled_for timestamptz not null,
    location      text,
    status        text        not null default 'open'
                              check (status in ('open', 'closed', 'played')),
    seats         int         not null default 6 check (seats >= 1),
    created_at    timestamptz not null default now()
);

create index if not exists sessions_scheduled_idx
    on sessions (scheduled_for desc);


-- ---------------------------------------------------------------------------
-- Sign-ups
-- ---------------------------------------------------------------------------

create table if not exists signups (
    id          uuid        primary key default gen_random_uuid(),
    session_id  uuid        not null references sessions (id) on delete cascade,
    player_id   uuid        not null references players (id) on delete cascade,
    note        text,
    created_at  timestamptz not null default now(),
    -- One sign-up per person per night. Signing up again updates, never duplicates.
    unique (session_id, player_id)
);

-- Which games that person fancies playing. Many-to-many: this is the vote.
create table if not exists signup_games (
    signup_id uuid not null references signups (id) on delete cascade,
    game_id   uuid not null references games (id)   on delete cascade,
    primary key (signup_id, game_id)
);


-- ---------------------------------------------------------------------------
-- Matches and ratings — the source of truth for the leaderboards
-- ---------------------------------------------------------------------------

create table if not exists matches (
    id         uuid        primary key default gen_random_uuid(),
    session_id uuid        references sessions (id) on delete set null,
    game_id    uuid        not null references games (id),
    played_at  timestamptz not null default now(),
    created_at timestamptz not null default now()
);

create table if not exists match_players (
    id             uuid   primary key default gen_random_uuid(),
    match_id       uuid   not null references matches (id) on delete cascade,
    player_id      uuid   not null references players (id),
    -- 1 = winner. Equal placements = a tie. Teammates share a placement.
    placement      int    not null check (placement >= 1),
    team           text,
    score          int,
    -- Written by the Elo engine. A cache, not the truth: these can always be
    -- recomputed by replaying matches in played_at order.
    rating_before  double precision,
    rating_after   double precision,
    unique (match_id, player_id)
);

-- Current rating per player per game. Also a cache. Also rebuildable.
create table if not exists ratings (
    player_id    uuid             not null references players (id) on delete cascade,
    game_id      uuid             not null references games (id)   on delete cascade,
    rating       double precision not null default 1000,
    games_played int              not null default 0,
    updated_at   timestamptz      not null default now(),
    primary key (player_id, game_id)
);


-- ---------------------------------------------------------------------------
-- Celebratory awards — the non-game titles (e.g. Professional Snacker).
-- award_types is the persistent registry the admin manages; session_awards
-- records who held each award on each night. Removing an award type cascades
-- its winners away.
-- ---------------------------------------------------------------------------

create table if not exists award_types (
    name       text        primary key,
    created_at timestamptz not null default now()
);

create table if not exists session_awards (
    session_id uuid        not null references sessions (id) on delete cascade,
    award      text        not null references award_types (name)
                           on update cascade on delete cascade,
    player_id  uuid        not null references players (id)  on delete cascade,
    created_at timestamptz not null default now(),
    primary key (session_id, award)
);
