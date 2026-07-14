-- Seed data. Edit this to your group's actual games, then run it in the Neon
-- SQL Editor. Re-running is safe — existing names are left alone.

insert into games (name, mode, min_players, max_players) values
    ('Catan',        'ffa',  3, 4),
    ('Codenames',    'team', 4, 8),
    ('Chess',        'duel', 2, 2),
    ('Wingspan',     'ffa',  2, 5),
    ('Ticket to Ride','ffa', 2, 5),
    ('Coup',         'ffa',  3, 6),
    ('Azul',         'ffa',  2, 4)
on conflict do nothing;

-- A first session so you have something to open. Change the date.
insert into sessions (slug, title, scheduled_for, location) values
    ('kickoff', 'The inaugural game night', now() + interval '7 days', 'Mine, 7pm')
on conflict (slug) do nothing;
