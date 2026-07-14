-- Every player who is still on the '🎲' default gets a distinct fun emoji.
--
-- Both sides are shuffled and paired by row number, so the assignment is random
-- but collision-free: each free emoji is handed to exactly one player. Emojis
-- already claimed by someone (not the default) are excluded from the pool, which
-- makes this safe to re-run after new players have been added.

with pool as (
    select unnest(array[
        '🦊','🐙','🦁','🐸','🦄','🐝','🦖','🐢','🦉','🐧',
        '🦩','🐳','🦋','🐨','🦔','🦥','🐬','🦜','🐊','🦭',
        '🦡','🐺','🦚','🐡','🐌','🦇','🐇','🦈','🐲','🦂',
        '🚀','🍕','🌮','🎸','👻','🤖','🧙','🍄','⚡','🎩',
        '🪐','🧊','🔥','🌵','🍩','🥑','🦞','🎺','🪩','🧲'
    ]) as emoji
),
free as (
    select emoji, row_number() over (order by random()) as rn
    from pool
    where emoji not in (select emoji from players where emoji <> '🎲')
),
targets as (
    select id, row_number() over (order by random()) as rn
    from players
    where emoji = '🎲'
)
update players p
   set emoji = free.emoji
  from targets
  join free on free.rn = targets.rn
 where p.id = targets.id;

-- The default is now only a fallback for rows inserted outside the app; the
-- app itself always supplies an emoji via pick_emoji().