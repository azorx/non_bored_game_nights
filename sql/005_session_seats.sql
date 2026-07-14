-- Migration 005: a seat cap per session.
-- Run this in the Neon SQL Editor. Safe to re-run.
--
-- Defaults to 6 — the size of the friend group this app was built for — for
-- every session that already exists. New sessions can set their own number
-- from the admin page.

alter table sessions add column if not exists seats int not null default 6
    check (seats >= 1);
