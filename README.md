# Non-Bored Game Nights

Players, game nights, and sign-ups. Elo and leaderboards come next.

Python + FastAPI + Jinja templates + raw SQL on Neon Postgres, deployed to Vercel.
No JavaScript, no build step, no server to maintain.

---

## 1. Create the database (5 minutes)

Neon Console → your project → **SQL Editor**.

1. Paste the whole of `sql/schema.sql`, run it.
2. Paste the whole of `sql/seed.sql`, run it. Edit the game list first if you like — you can also add games later from `/admin`.

Check it worked:

```sql
select table_name from information_schema.tables where table_schema = 'public';
```

You should see: `games`, `match_players`, `matches`, `players`, `ratings`, `sessions`, `signup_games`, `signups`.

## 2. Run it locally (10 minutes)

Clone the repo, drop these files in, then in the VSCode terminal:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Now open `.env` and paste in two things:

- `DATABASE_URL` — Neon Console → **Connect** → tick **Pooled connection**. The hostname must contain `-pooler`. This matters: serverless functions open and drop connections constantly, and the pooler is what stops Neon's connection limit from being the thing that takes your site down.
- `ADMIN_PASSCODE` — anything. It is the shared password for `/admin`.

Then:

```bash
uvicorn main:app --reload
```

Open http://localhost:8000 . If the homepage loads, you are done. If it does not, go straight to **http://localhost:8000/db-check** — it returns the actual database error as JSON instead of a generic 500.

## 3. Deploy (10 minutes)

```bash
git add .
git commit -m "Players, sessions and sign-ups"
git push
```

In Vercel: **Add New → Project → import `azorx/non_bored_game_nights`**.

- Framework preset: Vercel should detect **FastAPI** on its own (it reads `requirements.txt`). If it offers "Other", that is fine too.
- **Root Directory: leave completely empty.** Not `./`. This is the single most common cause of a failed first deploy.
- Before hitting Deploy, open **Environment Variables** and add `DATABASE_URL` and `ADMIN_PASSCODE` with the same values as your `.env`. Vercel cannot see your `.env` file — it is gitignored, which is correct.

Deploy. Then check, in this order:

| URL | What it proves |
|---|---|
| `/health` | FastAPI is running at all |
| `/db-check` | Vercel can reach Neon, and lists your tables |
| `/` | the whole thing works |

If `/health` works and `/db-check` fails, it is the `DATABASE_URL` env var. If `/health` itself 500s, it is a build error — read the Vercel build log.

## 4. Use it

1. Go to `/admin`, enter your passcode.
2. Create a game night. You get redirected straight to its page — that URL, e.g. `https://non-bored-game-nights.vercel.app/s/x7k2p9qa`, is what you paste into WhatsApp.
3. People type their name, tick the games they fancy, hit "I'm in".

---

## How sign-up works

There are no accounts and no passwords, on purpose.

Someone types "Dave". If a player called Dave (or dave, or DAVE) exists, the sign-up attaches to that player. If not, one is created. This is the entire user system, and it is the right amount for a friend group — every extra step between the WhatsApp link and being signed up costs you attendance.

The cost is that anyone can sign up as anyone. In a group of friends, this is not a threat model; it is a prank at worst. Ratings are entered by whoever runs the night from `/admin`, so nobody can fake a result.

Signing up twice **updates** your entry rather than duplicating it (`unique (session_id, player_id)` plus an upsert), so changing your mind about games is just submitting the form again.

## Files

```
main.py              all routes
db.py                three functions: fetch_one, fetch_all, execute
elo.py               the rating engine (not wired in yet)
test_elo.py          24 tests for it
templates/           Jinja HTML
sql/schema.sql       run once in Neon
sql/seed.sql         some starter games
requirements.txt     Vercel reads this to detect FastAPI
.python-version      pins Python 3.12
.env                 your secrets — gitignored, never commit
```

## Next

- Wire `elo.py` into a results-entry screen at `/admin/matches/new`
- `/leaderboards` — one tab per game
- `/players/{name}` — rating history
 