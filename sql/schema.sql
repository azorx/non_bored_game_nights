-- Non-Bored Game Nights — schema
-- Run this once in the Neon SQL Editor (Console -> your project -> SQL Editor).
-- Safe to re-run: everything is IF NOT EXISTS.
--
-- Phases 1-2 use: players, games, sessions, signups, signup_games.
-- The match tables at the bottom are unused for now, but they are cheap to
-- create and defining them up front saves a migration later.

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
    id           uuid        primary key default gen_random_uuid(),
    name         text        not null,
    -- 'ffa'  = free-for-all, everyone finishes in a position
    -- 'duel' = strictly two sides (chess, backgammon)
    -- 'team' = teams of players finish in a position
    mode         text        not null default 'ffa'
                             check (mode in ('ffa', 'duel', 'team')),
    min_players  int         not null default 2 check (min_players >= 2),
    max_players  int         not null default 8,
    active       boolean     not null default true,
    created_at   timestamptz not null default now(),
    check (max_players >= min_players)
);

create unique index if not exists games_name_lower_idx
    on games (lower(name));


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
-- Matches — not used until Phase 3, defined now to avoid a later migration
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
