-- ===========================================================================
-- RESET — wipe ALL data and start the website for real.
-- ===========================================================================
--
--   ⚠️  DESTRUCTIVE AND IRREVERSIBLE. This deletes every player, game, session,
--       sign-up, match and rating. It cannot be undone. Run it exactly once,
--       when you are done testing and ready to go live.
--
-- It does NOT touch the schema — the tables, indexes and the games_summary view
-- all stay. Only the rows go. Afterwards the database is empty and clean: add
-- your real games from /admin/games and open your first night from /admin.
--
-- Run it in the Neon SQL Editor (Console -> your project -> SQL Editor).
--
-- Why TRUNCATE and not DELETE: TRUNCATE clears the tables in one shot and,
-- with CASCADE, walks the foreign keys for you so order does not matter. Every
-- table is listed explicitly anyway, so it is obvious exactly what is being
-- emptied and nothing is cleared by surprise.

truncate table
    match_players,   -- who finished where in each match
    matches,         -- the matches themselves (the source of every rating)
    ratings,         -- the leaderboard cache (rebuilt from matches)
    signup_games,    -- game votes
    signups,         -- who signed up for which night
    sessions,        -- the game nights
    games,           -- the games on offer
    players          -- everyone
restart identity cascade;

-- Nothing is seeded back in on purpose: you are starting from a blank slate.
-- Add your games and your first night through the admin pages.
