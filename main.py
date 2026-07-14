"""
Non-Bored Game Nights.

Players, sessions, sign-ups.

Vercel finds this file automatically (it looks for main.py / index.py / app.py /
server.py) and finds the `app` variable inside it. There is no deployment config
to write.

Route map
---------
  GET  /                    what's next, who's coming
  GET  /players             everyone who exists (read-only)
  GET  /s/{slug}            THE WHATSAPP LINK — sign-up page for one game night
  POST /s/{slug}/signup     sign up (find-or-create player, record game votes)
  POST /s/{slug}/withdraw   drop out
  GET  /admin               create sessions and games (passcode required)
  GET  /health              is the app up (touches no database)
  GET  /db-check            can the app reach Neon
"""

import hashlib
import io
import os
import random
import secrets
import string
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image

import db
import elo

load_dotenv()  # Reads .env locally. On Vercel, env vars come from the dashboard.

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()

# Absolute path, not "templates". A serverless function does not always run with
# the working directory you expect; a relative path is the single most common
# way something works locally and breaks on deploy.
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ADMIN_COOKIE = "gn_admin"

# How many games each person must vote for. Two is the sweet spot: one vote is
# just "my favourite" and tells you nothing about what the group can agree on;
# two forces a second preference and makes the tally actually mean something.
MIN_VOTES = 2

# Co-op leaderboards rank by win rate, and a win rate is only meaningful once
# there is a handful of games behind it — one lucky win should not sit at 100%
# above everyone. Below this, a player is shown as "still qualifying".
COOP_MIN_GAMES = 3

# Human-readable game modes, in one place so every template says the same thing.
MODE_LABELS = {
    "ffa": "free-for-all",
    "duel": "head to head",
    "team": "teams",
    "coop": "cooperative",
}


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)

# Uploads are resized to this width before being stored. Nobody needs a 4000px
# board game box shot on a phone, and it keeps the database small.
IMAGE_MAX_WIDTH = 800
IMAGE_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Every player gets one of these, picked at random and never shared with another
# player, so you can pick someone out of a sign-up list at a glance. They are
# deliberately silhouette-distinct: no two brown four-legged animals, no near
# duplicates that blur together at 16px on a phone.
PLAYER_EMOJIS = [
    "🦊", "🐙", "🦁", "🐸", "🦄", "🐝", "🦖", "🐢", "🦉", "🐧",
    "🦩", "🐳", "🦋", "🐨", "🦔", "🦥", "🐬", "🦜", "🐊", "🦭",
    "🦡", "🐺", "🦚", "🐡", "🐌", "🦇", "🐇", "🦈", "🐲", "🦂",
    "🚀", "🍕", "🌮", "🎸", "👻", "🤖", "🧙", "🍄", "⚡", "🎩",
    "🪐", "🧊", "🔥", "🌵", "🍩", "🥑", "🦞", "🎺", "🪩", "🧲",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def redirect(url: str) -> RedirectResponse:
    """303 means "I handled your POST, now go GET this page". It is what stops
    the browser re-submitting the form when someone hits refresh."""
    return RedirectResponse(url, status_code=303)


def make_slug(length: int = 8) -> str:
    """A short URL token. Not a security boundary — just short enough to look
    tidy in WhatsApp and random enough that nobody stumbles onto it."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_admin(request: Request) -> bool:
    """Is this request coming from someone who has entered the passcode?

    Registered as a Jinja global below, so any template can ask. This is purely
    a *display* question — it decides whether to draw an Edit button. It is not
    what protects anything. The protection is require_admin on the routes
    themselves, which runs on the server before the function body does. Hiding a
    button never secures anything; the route guard does.
    """
    expected = os.environ.get("ADMIN_PASSCODE", "")
    supplied = request.cookies.get(ADMIN_COOKIE, "")
    return bool(expected) and secrets.compare_digest(supplied, expected)


def require_admin(request: Request) -> None:
    """Guards the admin pages. One shared passcode for the whole group.

    secrets.compare_digest rather than == so the comparison takes the same time
    whatever the input. Overkill here, but it is free and it is a good habit.
    """
    if not is_admin(request):
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})


# Lets every template call is_admin(request) to decide what to draw.
templates.env.globals["is_admin"] = is_admin

# One spelling of each game mode, shared by every template.
templates.env.globals["mode_label"] = mode_label


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_session_by_slug(slug: str) -> dict:
    row = db.fetch_one("select * from sessions where slug = %s", [slug])
    if row is None:
        raise HTTPException(status_code=404, detail="No such game night")
    return row


def get_signups(session_id) -> list[dict]:
    """Everyone signed up, each with their list of game votes.

    array_agg collapses each player's votes into one row, so this is a single
    query rather than one query per player.
    """
    return db.fetch_all(
        """
        select
            s.id       as signup_id,
            s.note,
            s.created_at,
            p.id       as player_id,
            p.name,
            p.emoji,
            coalesce(
                array_agg(g.name order by g.name) filter (where g.id is not null),
                '{}'
            ) as game_names
        from signups s
        join players p on p.id = s.player_id
        left join signup_games sg on sg.signup_id = s.id
        left join games g on g.id = sg.game_id
        where s.session_id = %s
        group by s.id, p.id
        order by s.created_at
        """,
        [session_id],
    )


def get_vote_tally(session_id) -> list[dict]:
    """Which games are winning the vote for this night."""
    return db.fetch_all(
        """
        select g.name, g.min_players, g.max_players, count(*) as votes
        from signup_games sg
        join signups s on s.id = sg.signup_id
        join games   g on g.id = sg.game_id
        where s.session_id = %s
        group by g.id
        order by votes desc, g.name
        """,
        [session_id],
    )


def active_games() -> list[dict]:
    """Reads the view, not the table, so the image bytes are never pulled back
    just to render a list of names."""
    return db.fetch_all(
        "select * from games_summary where active order by lower(name)"
    )


def parse_duration(raw: str) -> int | None:
    """An empty duration box means "I don't know", which is a real answer and
    stores as NULL. A nonsense one is rejected rather than silently coerced."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Duration must be a whole number of minutes")
    if not 1 <= minutes <= 600:
        raise HTTPException(status_code=400, detail="Duration must be between 1 and 600 minutes")
    return minutes


def process_upload(upload: UploadFile | None) -> tuple[bytes, str] | None:
    """Resize an uploaded image and hand back (bytes, mime type).

    Returns None if no file was actually chosen — an empty file input still
    arrives, it just has no filename.
    """
    if upload is None or not upload.filename:
        return None

    raw = upload.file.read()
    if not raw:
        return None
    if len(raw) > IMAGE_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That image is too big (8MB max)")

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(status_code=400, detail="That does not look like an image")

    # Phone photos carry an orientation flag rather than being rotated on disk;
    # without this, half of them render sideways.
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    if img.width > IMAGE_MAX_WIDTH:
        height = round(img.height * IMAGE_MAX_WIDTH / img.width)
        img = img.resize((IMAGE_MAX_WIDTH, height), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue(), "image/jpeg"


def pick_emoji() -> str:
    """Pick a fun emoji nobody else has yet.

    Uniqueness is best-effort, not a database constraint: once the pool runs
    out (fifty players deep) we start reusing rather than refusing to create
    the player. A duplicate icon is a much smaller problem than a failed
    sign-up on a Friday night.
    """
    taken = {row["emoji"] for row in db.fetch_all("select emoji from players")}
    unclaimed = [e for e in PLAYER_EMOJIS if e not in taken]
    return random.choice(unclaimed or PLAYER_EMOJIS)


def find_or_create_player(name: str) -> dict:
    """The heart of frictionless sign-up: a typed name is enough.

    Matching is case-insensitive, so "dave" tonight and "Dave" next month are
    the same person. ON CONFLICT keeps it safe even if two people submit at the
    same instant — and, because the conflict branch only touches the name, a
    returning player keeps the emoji they already had.
    """
    name = " ".join(name.split())  # collapse stray whitespace
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="That name is too long")

    existing = db.fetch_one(
        "select * from players where lower(name) = lower(%s)", [name]
    )
    if existing:
        return existing

    return db.fetch_one(
        """
        insert into players (name, emoji) values (%s, %s)
        on conflict (lower(name)) do update set name = players.name
        returning *
        """,
        [name, pick_emoji()],
    )


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------


@app.get("/")
def home(request: Request):
    upcoming = db.fetch_all(
        """
        select s.*, count(su.id) as signup_count
        from sessions s
        left join signups su on su.session_id = s.id
        where s.status = 'open'
          and s.scheduled_for > now() - interval '12 hours'
        group by s.id
        order by s.scheduled_for
        """
    )
    player_count = db.fetch_one("select count(*) as n from players")["n"]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"upcoming": upcoming, "player_count": player_count},
    )


@app.get("/players")
def players_page(request: Request, deleted: str | None = None):
    """Read-only roster for everyone; the admin also gets a delete button per
    player. Players come into existence by signing up for a night, so there is
    nothing to create here — this page just shows who exists."""
    people = db.fetch_all(
        """
        select p.*, count(s.id) as nights_attended
        from players p
        left join signups s on s.player_id = p.id
        group by p.id
        order by lower(p.name)
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="players.html",
        context={"players": people, "deleted": deleted},
    )


def render_session(
    request: Request,
    session: dict,
    joined: str | None = None,
    error: str | None = None,
    form_name: str = "",
    form_note: str = "",
    form_games: list[str] | None = None,
):
    """One place that builds the session page, so a failed sign-up can re-render
    it with the error and the person's answers still filled in — rather than
    dumping them on a blank error page and making them start again."""
    return templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "session": session,
            "signups": get_signups(session["id"]),
            "tally": get_vote_tally(session["id"]),
            "games": active_games(),
            "players": db.fetch_all("select name from players order by lower(name)"),
            "joined": joined,
            "error": error,
            "min_votes": MIN_VOTES,
            "form_name": form_name,
            "form_note": form_note,
            "form_games": form_games or [],
        },
    )


@app.get("/games")
def games_page(request: Request):
    """Public, read-only. What we play, with pictures and how long each takes.

    Deliberately a separate route from /admin/games rather than the same page
    with the buttons hidden: a page that renders edit forms and then hides them
    is one CSS mistake away from showing them, and it still has to fetch data it
    should not be handing out.
    """
    return templates.TemplateResponse(
        request=request,
        name="games.html",
        context={
            "games": db.fetch_all(
                "select * from games_summary where active order by lower(name)"
            )
        },
    )


def _coop_standings(game_id) -> tuple[list[dict], list[dict]]:
    """Win-rate board for a co-op game.

    Co-op matches have no ratings, so this reads wins and plays straight from the
    results. A win rate needs a few games behind it to mean anything, so players
    split into qualifiers (ranked by win rate) and everyone still building up a
    record. Placement 1 is a win, 2 is a loss — the whole table shares it.
    """
    rows = db.fetch_all(
        """
        select p.name, p.emoji,
               count(*) as plays,
               count(*) filter (where mp.placement = 1) as wins
        from match_players mp
        join matches m on m.id = mp.match_id
        join players p on p.id = mp.player_id
        where m.game_id = %s
        group by p.id
        """,
        [game_id],
    )
    for r in rows:
        r["win_rate"] = r["wins"] / r["plays"] if r["plays"] else 0.0

    qualifiers = sorted(
        (r for r in rows if r["plays"] >= COOP_MIN_GAMES),
        key=lambda r: (-r["win_rate"], -r["wins"], r["name"].lower()),
    )
    provisional = sorted(
        (r for r in rows if r["plays"] < COOP_MIN_GAMES),
        key=lambda r: (-r["plays"], r["name"].lower()),
    )
    return qualifiers, provisional


def _title_by_avg_rating(mode: str) -> dict | None:
    """Holder of a category belt: the highest mean rating across every game of
    one mode. One game counts — its rating is the mean of one number."""
    return db.fetch_one(
        """
        select p.name, p.emoji, avg(r.rating) as avg_rating, count(*) as games
        from ratings r
        join players p on p.id = r.player_id
        join games g on g.id = r.game_id
        where g.mode = %s
        group by p.id
        order by avg_rating desc, count(*) desc, lower(p.name)
        limit 1
        """,
        [mode],
    )


def compute_titles() -> list[dict]:
    """Every belt currently held. Three category titles across the competitive
    modes, plus a "<Game> Master" for each individual game — top rating for a
    competitive game, best win rate for a co-op one. Only held belts are
    returned; a game nobody has played yet grants no title."""
    titles: list[dict] = []

    categories = [
        ("Duelist", "Best at 1v1", "duel"),
        ("Brawler", "Best at free-for-all", "ffa"),
        ("Team Player", "Best at team games", "team"),
    ]
    for name, subtitle, mode in categories:
        holder = _title_by_avg_rating(mode)
        if holder:
            titles.append(
                {
                    "title": name,
                    "subtitle": subtitle,
                    "holder": holder,
                    "detail": f"{round(holder['avg_rating'])} avg rating",
                }
            )

    # Celebratory, non-game titles — every registered award, in order. Persistent:
    # an award with no winner yet still appears, unclaimed. The belt goes to
    # whoever has won it on the most nights. New awards the admin registers show
    # up here with no code change at all.
    for row in db.fetch_all(
        """
        select at.name as award, w.name as holder_name,
               w.emoji as holder_emoji, w.wins
        from award_types at
        left join lateral (
            select p.name, p.emoji, count(*) as wins
            from session_awards a
            join players p on p.id = a.player_id
            where a.award = at.name
            group by p.id
            order by count(*) desc, lower(p.name)
            limit 1
        ) w on true
        order by lower(at.name)
        """
    ):
        holder = (
            {"name": row["holder_name"], "emoji": row["holder_emoji"]}
            if row["holder_name"]
            else None
        )
        titles.append(
            {
                "title": row["award"],
                "subtitle": "Most nights awarded",
                "holder": holder,
                "detail": (
                    f"{row['wins']} night{'' if row['wins'] == 1 else 's'}"
                    if holder
                    else None
                ),
            }
        )

    for g in db.fetch_all(
        "select id, name, mode from games_summary order by lower(name)"
    ):
        if g["mode"] == "coop":
            holder = db.fetch_one(
                """
                select p.name, p.emoji,
                       count(*) filter (where mp.placement = 1) as wins,
                       count(*) as plays
                from match_players mp
                join matches m on m.id = mp.match_id
                join players p on p.id = mp.player_id
                where m.game_id = %s
                group by p.id
                having count(*) >= %s
                order by (count(*) filter (where mp.placement = 1))::float
                         / count(*) desc, wins desc, lower(p.name)
                limit 1
                """,
                [g["id"], COOP_MIN_GAMES],
            )
            detail = (
                f"{round(100 * holder['wins'] / holder['plays'])}% wins"
                if holder
                else None
            )
        else:
            holder = db.fetch_one(
                """
                select p.name, p.emoji, r.rating
                from ratings r
                join players p on p.id = r.player_id
                where r.game_id = %s
                order by r.rating desc, r.games_played desc, lower(p.name)
                limit 1
                """,
                [g["id"]],
            )
            detail = f"{round(holder['rating'])} rating" if holder else None

        if holder:
            titles.append(
                {
                    "title": f"{g['name']} Master",
                    "subtitle": None,
                    "holder": holder,
                    "detail": detail,
                }
            )

    return titles


@app.get("/leaderboard")
def leaderboard_page(
    request: Request, game: str | None = None, saved: str | None = None
):
    """One board per game, chosen from a dropdown, plus the table of titles.

    The game list is every game, not just the active ones: a retired game keeps
    its board forever, so it must stay selectable here even though it has dropped
    off the sign-up page. With nothing chosen we default to the first game so the
    page is never a bare dropdown.

    A competitive game shows its Elo ladder; a co-op game shows a win-rate board
    instead — same page, different maths, decided by the game's mode.
    """
    games = db.fetch_all(
        "select id, name, mode, has_image from games_summary order by lower(name)"
    )
    selected_id = game or (games[0]["id"] if games else None)

    selected = None
    standings: list[dict] = []
    provisional: list[dict] = []
    recent: list[dict] = []
    is_coop = False
    if selected_id is not None:
        selected = db.fetch_one(
            "select * from games_summary where id = %s", [selected_id]
        )
        if selected is None:
            raise HTTPException(status_code=404, detail="No such game")
        is_coop = selected["mode"] == "coop"

        if is_coop:
            standings, provisional = _coop_standings(selected_id)
        else:
            standings = db.fetch_all(
                """
                select p.name, p.emoji, r.rating, r.games_played
                from ratings r
                join players p on p.id = r.player_id
                where r.game_id = %s
                order by r.rating desc, r.games_played desc, lower(p.name)
                """,
                [selected_id],
            )

        # The last few results, each collapsed into one row via json_agg so the
        # template gets a match with its players already attached — with the
        # rating swing for competitive games (null for co-op, which just won or
        # lost as a table).
        recent = db.fetch_all(
            """
            select m.played_at,
                   json_agg(
                       json_build_object(
                           'name', p.name,
                           'emoji', p.emoji,
                           'placement', mp.placement,
                           'team', mp.team,
                           'delta', mp.rating_after - mp.rating_before
                       ) order by mp.placement
                   ) as players
            from matches m
            join match_players mp on mp.match_id = m.id
            join players p on p.id = mp.player_id
            where m.game_id = %s
            group by m.id
            order by m.played_at desc, m.created_at desc
            limit 5
            """,
            [selected_id],
        )

    return templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context={
            "games": games,
            "selected": selected,
            "standings": standings,
            "provisional": provisional,
            "recent": recent,
            "is_coop": is_coop,
            "coop_min_games": COOP_MIN_GAMES,
            "titles": compute_titles(),
            "saved": saved,
        },
    )


@app.get("/s/{slug}")
def session_page(request: Request, slug: str, joined: str | None = None):
    """This is the URL you paste into WhatsApp."""
    return render_session(request, get_session_by_slug(slug), joined=joined)


@app.get("/games/{game_id}/image")
def game_image(game_id: str, request: Request):
    """Serves a game's picture straight out of Postgres.

    The URL for a game's image never changes, so we can't cache it hard by time:
    that is exactly what made a re-uploaded picture appear not to save — the
    browser and CDN kept serving the old bytes from a still-valid cache for up to
    an hour. Instead the cache is keyed on the *content*: an ETag that is a hash
    of the image bytes. Browsers revalidate on every view, but when nothing has
    changed that costs a tiny 304 with no body, and the instant you upload a new
    picture the hash changes and the new image is served at once.
    """
    row = db.fetch_one(
        "select image, image_type from games where id = %s", [game_id]
    )
    if not row or row["image"] is None:
        raise HTTPException(status_code=404, detail="No picture for that game")

    data = bytes(row["image"])
    etag = '"' + hashlib.md5(data).hexdigest() + '"'
    cache_headers = {"ETag": etag, "Cache-Control": "public, max-age=0, must-revalidate"}

    # The browser sends back the ETag it holds; if it still matches, it already
    # has the right image and we send nothing but "you're up to date".
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)

    return Response(
        content=data,
        media_type=row["image_type"] or "image/jpeg",
        headers=cache_headers,
    )


@app.post("/s/{slug}/signup")
async def signup(request: Request, slug: str):
    """Sign up, or update an existing sign-up.

    Reads the raw form rather than declaring parameters, because the game
    checkboxes arrive as a repeated field (game_ids=..&game_ids=..) and we want
    every one of them.
    """
    session = get_session_by_slug(slug)
    if session["status"] != "open":
        raise HTTPException(status_code=409, detail="Sign-ups are closed for this night")

    form = await request.form()
    name = (form.get("name") or "").strip()
    note = (form.get("note") or "").strip() or None
    game_ids = form.getlist("game_ids")

    # Enforced here, on the server, not just in the browser. The JavaScript on
    # the page stops most people from submitting too few, but a form can always
    # be submitted with JS disabled, and this is the check that actually holds.
    if len(game_ids) < MIN_VOTES:
        return render_session(
            request,
            session,
            error=f"Pick at least {MIN_VOTES} games — that is what makes the vote useful.",
            form_name=name,
            form_note=note or "",
            form_games=game_ids,
        )

    # Seats are first-come-first-served, but a returning player editing their
    # own sign-up must never be locked out by a full session — only a brand
    # new sign-up competes for the seats that are left. Looked up by name
    # rather than via find_or_create_player, so a rejected sign-up never
    # creates a player row for someone who didn't get a seat.
    existing_player = db.fetch_one(
        "select * from players where lower(name) = lower(%s)",
        [" ".join(name.split())],
    )
    already_in = existing_player and db.fetch_one(
        "select 1 from signups where session_id = %s and player_id = %s",
        [session["id"], existing_player["id"]],
    )
    if not already_in:
        taken = db.fetch_one(
            "select count(*) as n from signups where session_id = %s",
            [session["id"]],
        )["n"]
        if taken >= session["seats"]:
            return render_session(
                request,
                session,
                error="Sorry — all seats are taken for this night.",
                form_name=name,
                form_note=note or "",
                form_games=game_ids,
            )

    player = find_or_create_player(name)

    # Upsert: signing up twice edits your entry rather than duplicating it.
    row = db.fetch_one(
        """
        insert into signups (session_id, player_id, note)
        values (%s, %s, %s)
        on conflict (session_id, player_id)
        do update set note = excluded.note
        returning id
        """,
        [session["id"], player["id"], note],
    )
    signup_id = row["id"]

    # Replace the votes wholesale — the simplest correct way to let someone
    # change their mind.
    db.execute("delete from signup_games where signup_id = %s", [signup_id])
    for game_id in game_ids:
        db.execute(
            "insert into signup_games (signup_id, game_id) values (%s, %s) "
            "on conflict do nothing",
            [signup_id, game_id],
        )

    return redirect(f"/s/{slug}?joined={player['name']}")


@app.post("/s/{slug}/withdraw")
def withdraw(slug: str, player_id: str = Form(...)):
    session = get_session_by_slug(slug)
    db.execute(
        "delete from signups where session_id = %s and player_id = %s",
        [session["id"], player_id],
    )
    return redirect(f"/s/{slug}")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@app.get("/admin/login")
def admin_login_form(request: Request, bad: str | None = None):
    return templates.TemplateResponse(
        request=request, name="admin_login.html", context={"bad": bad}
    )


@app.post("/admin/login")
def admin_login(passcode: str = Form(...)):
    expected = os.environ.get("ADMIN_PASSCODE", "")
    if not expected or not secrets.compare_digest(passcode, expected):
        return redirect("/admin/login?bad=1")

    response = redirect("/admin")
    # httponly: JavaScript cannot read it. samesite=lax: not sent from other
    # sites' forms. Both are free, so there is no reason not to.
    response.set_cookie(
        ADMIN_COOKIE,
        expected,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/admin/logout")
def admin_logout():
    """Drop the admin cookie.

    Deliberately a POST, not a GET: a GET that changes state can be triggered by
    anything that fetches a URL — a link preview, a prefetcher, an <img> tag on
    some other site. Logging out is harmless enough that it barely matters here,
    but "actions are POSTs, reads are GETs" is a rule worth keeping unbroken.
    """
    response = redirect("/")
    response.delete_cookie(ADMIN_COOKIE)
    return response


@app.get("/admin", dependencies=[Depends(require_admin)])
def admin_page(request: Request, deleted: str | None = None):
    sessions = db.fetch_all(
        """
        select s.*, count(su.id) as signup_count
        from sessions s
        left join signups su on su.session_id = s.id
        group by s.id
        order by s.scheduled_for desc
        """
    )

    # Award types are managed independently in the registry below; each one is a
    # winner field on every night's card.
    award_types = [
        r["name"]
        for r in db.fetch_all("select name from award_types order by lower(name)")
    ]
    # Who currently holds each award on each night: {session_id: {award: player}}.
    winners: dict = {}
    for r in db.fetch_all("select session_id, award, player_id from session_awards"):
        winners.setdefault(r["session_id"], {})[r["award"]] = r["player_id"]
    for s in sessions:
        s["award_winners"] = winners.get(s["id"], {})

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "sessions": sessions,
            "games": active_games(),
            "players": db.fetch_all(
                "select id, name, emoji from players order by lower(name)"
            ),
            "award_types": award_types,
            "deleted": deleted,
        },
    )


@app.post("/admin/sessions", dependencies=[Depends(require_admin)])
def create_session(
    title: str = Form(...),
    scheduled_for: str = Form(...),  # from <input type="datetime-local">
    location: str = Form(""),
    seats: int = Form(6),
):
    if not 1 <= seats <= 100:
        raise HTTPException(status_code=400, detail="Seats must be between 1 and 100")
    when = datetime.fromisoformat(scheduled_for)
    slug = make_slug()
    db.execute(
        "insert into sessions (slug, title, scheduled_for, location, seats) "
        "values (%s, %s, %s, %s, %s)",
        [slug, title.strip(), when, location.strip() or None, seats],
    )
    return redirect(f"/s/{slug}")


@app.post("/admin/sessions/{session_id}/status", dependencies=[Depends(require_admin)])
def set_session_status(session_id: str, status: str = Form(...)):
    if status not in {"open", "closed", "played"}:
        raise HTTPException(status_code=400, detail="Unknown status")
    db.execute("update sessions set status = %s where id = %s", [status, session_id])
    return redirect("/admin")


@app.post("/admin/awards", dependencies=[Depends(require_admin)])
def add_award_type(name: str = Form(...)):
    """Register a persistent award. It immediately becomes a winner field on
    every night's card and a title on the leaderboard — unclaimed until someone
    wins it. The name doubles as the title shown on the leaderboard."""
    name = " ".join((name or "").split())  # collapse stray whitespace
    if not name:
        raise HTTPException(status_code=400, detail="An award needs a name")
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="That award name is too long")
    db.execute(
        "insert into award_types (name) values (%s) on conflict do nothing", [name]
    )
    return redirect("/admin")


@app.post("/admin/awards/delete", dependencies=[Depends(require_admin)])
def delete_award_type(name: str = Form(...)):
    """Remove an award from the registry. Its recorded winners cascade away with
    it, so it disappears from the leaderboard and every session card at once."""
    db.execute("delete from award_types where name = %s", [name])
    return redirect("/admin")


@app.post("/admin/sessions/{session_id}/award", dependencies=[Depends(require_admin)])
def set_session_award(
    session_id: str, award: str = Form(...), player_id: str = Form("")
):
    """Record, change or clear who won a registered award on one night.

    The award must already exist in the registry (the field is only rendered for
    registered awards, and the foreign key enforces it). An empty player means
    "nobody won it tonight" and clears any previous pick, so a mistaken tap is
    easy to undo.
    """
    award = " ".join((award or "").split())  # collapse stray whitespace
    if not award:
        raise HTTPException(status_code=400, detail="An award needs a name")

    player_id = (player_id or "").strip()
    if player_id:
        db.execute(
            """
            insert into session_awards (session_id, award, player_id)
            values (%s, %s, %s)
            on conflict (session_id, award)
            do update set player_id = excluded.player_id
            """,
            [session_id, award, player_id],
        )
    else:
        db.execute(
            "delete from session_awards where session_id = %s and award = %s",
            [session_id, award],
        )
    return redirect("/admin")


@app.post("/admin/sessions/{session_id}/delete", dependencies=[Depends(require_admin)])
def delete_session(session_id: str):
    """Delete a game night — and, as asked, the results played that night with it.

    A match normally outlives its night (matches.session_id is ON DELETE SET
    NULL, so a deleted night would otherwise just orphan its matches and keep
    the ratings). Here we deliberately delete those matches first, then rebuild
    every ladder they touched so the leaderboards forget the night entirely, as
    if it never happened. Sign-ups and votes for the night cascade away on their
    own.
    """
    # Which ladders will need rebuilding once this night's matches are gone.
    affected = db.fetch_all(
        "select distinct game_id from matches where session_id = %s", [session_id]
    )
    # match_players cascades from matches; ratings are rebuilt below from scratch.
    db.execute("delete from matches where session_id = %s", [session_id])
    db.execute("delete from sessions where id = %s", [session_id])
    for row in affected:
        elo.recompute_game(row["game_id"])
    return redirect("/admin?deleted=night")


@app.post("/admin/players/{player_id}/delete", dependencies=[Depends(require_admin)])
def delete_player(player_id: str):
    """Remove a player completely — test accounts, duplicates, whoever.

    A player's match results have no cascade (that is on purpose: you can't
    casually delete someone who is woven into other people's history), so we
    clear those by hand, remember which games they touched, then delete the
    player. Their sign-ups, votes and ratings cascade. Finally every affected
    ladder is replayed, because a match that used to have this person in it is
    now a smaller match and everyone else's rating for it shifts accordingly.
    """
    affected = db.fetch_all(
        "select distinct m.game_id from match_players mp "
        "join matches m on m.id = mp.match_id where mp.player_id = %s",
        [player_id],
    )
    db.execute("delete from match_players where player_id = %s", [player_id])
    db.execute("delete from players where id = %s", [player_id])
    for row in affected:
        elo.recompute_game(row["game_id"])
    return redirect("/players?deleted=1")


@app.post("/admin/games", dependencies=[Depends(require_admin)])
def create_game(
    name: str = Form(...),
    mode: str = Form("ffa"),
    min_players: int = Form(2),
    max_players: int = Form(8),
    description: str = Form(""),
    duration_minutes: str = Form(""),
    picture: UploadFile | None = File(None),
):
    image = process_upload(picture)
    db.execute(
        """
        insert into games (name, mode, min_players, max_players,
                           description, duration_minutes, image, image_type)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict do nothing
        """,
        [
            name.strip(), mode, min_players, max_players,
            description.strip() or None,
            parse_duration(duration_minutes),
            image[0] if image else None,
            image[1] if image else None,
        ],
    )
    return redirect("/admin/games")


@app.get("/admin/games", dependencies=[Depends(require_admin)])
def admin_games(
    request: Request, deleted: str | None = None, retired: str | None = None
):
    return templates.TemplateResponse(
        request=request,
        name="admin_games.html",
        context={
            "games": db.fetch_all(
                """
                select gs.*, count(m.id) as times_played
                from games_summary gs
                left join matches m on m.game_id = gs.id
                group by gs.id, gs.name, gs.mode, gs.min_players, gs.max_players,
                         gs.active, gs.description, gs.duration_minutes,
                         gs.created_at, gs.has_image
                order by lower(gs.name)
                """
            ),
            "deleted": deleted,
            "retired": retired,
        },
    )


@app.post("/admin/games/{game_id}/delete", dependencies=[Depends(require_admin)])
def delete_game(request: Request, game_id: str):
    """Delete a game — but only if it has never been played.

    A game with matches against it is load-bearing history: its results feed
    every player's rating for that game, and the rating engine replays those
    matches. Deleting it would take that with it. So the moment a game has been
    played, the honest answer to "delete this" is "no, but you can retire it" —
    unticking `active` removes it from every future sign-up page while leaving
    the leaderboard and the history intact.

    signup_games and ratings cascade, so an unplayed game deletes cleanly even
    if people had already voted for it.
    """
    played = db.fetch_one(
        "select count(*) as n from matches where game_id = %s", [game_id]
    )["n"]

    if played:
        game = db.fetch_one("select name from games where id = %s", [game_id])
        db.execute("update games set active = false where id = %s", [game_id])
        return redirect(
            f"/admin/games?retired={game['name'] if game else 'That game'}"
        )

    db.execute("delete from games where id = %s", [game_id])
    return redirect("/admin/games?deleted=1")


@app.post("/admin/games/{game_id}", dependencies=[Depends(require_admin)])
def update_game(
    game_id: str,
    name: str = Form(...),
    mode: str = Form("ffa"),
    min_players: int = Form(2),
    max_players: int = Form(8),
    description: str = Form(""),
    duration_minutes: str = Form(""),
    active: str = Form(""),
    picture: UploadFile | None = File(None),
):
    db.execute(
        """
        update games
           set name = %s, mode = %s, min_players = %s, max_players = %s,
               description = %s, duration_minutes = %s, active = %s
         where id = %s
        """,
        [
            name.strip(), mode, min_players, max_players,
            description.strip() or None,
            parse_duration(duration_minutes),
            active == "on",
            game_id,
        ],
    )

    # Only touch the image if a new one was actually chosen. Submitting the form
    # with an empty file input must not wipe the existing picture.
    image = process_upload(picture)
    if image:
        db.execute(
            "update games set image = %s, image_type = %s where id = %s",
            [image[0], image[1], game_id],
        )

    return redirect("/admin/games")


# ---------------------------------------------------------------------------
# Results — turning a game night into ratings
# ---------------------------------------------------------------------------


@app.get("/admin/results", dependencies=[Depends(require_admin)])
def results_form(request: Request):
    """Enter what happened at the table: who played a game and where they
    finished. Active games only — you record results against games you still
    offer; a retired game's history is read-only from here on."""
    return templates.TemplateResponse(
        request=request,
        name="admin_results.html",
        context={
            "games": active_games(),
            "players": db.fetch_all("select name from players order by lower(name)"),
            "sessions": db.fetch_all(
                "select id, title, scheduled_for from sessions "
                "order by scheduled_for desc limit 20"
            ),
        },
    )


@app.post("/admin/results", dependencies=[Depends(require_admin)])
async def record_results(request: Request):
    """Record one game's result and rebuild that game's board.

    A competitive game replays its Elo ladder; a co-op game just banks the shared
    win or loss (handled early, below). Read from the raw form because the player
    rows arrive as parallel repeated fields (player_name=..&placement=..&team=..).
    Rows with a blank name are dropped, so the admin can leave spare rows lying
    around in the form.

    Players are found-or-created by typed name, exactly like sign-up: someone can
    appear in a result without ever having formally signed up for the night.
    """
    form = await request.form()
    game_id = form.get("game_id")
    session_id = (form.get("session_id") or "").strip() or None

    game = db.fetch_one("select id, name, mode from games where id = %s", [game_id])
    if game is None:
        raise HTTPException(status_code=400, detail="Pick a game")

    # Co-op is its own thing: the whole table wins or loses together, there are
    # no positions and no Elo. Record everyone with the shared outcome (placement
    # 1 = won, 2 = lost) and stop — the win-rate board reads straight from these.
    if game["mode"] == "coop":
        outcome = form.get("coop_outcome")
        if outcome not in {"won", "lost"}:
            raise HTTPException(
                status_code=400, detail="Say whether the table won or lost"
            )
        placement = 1 if outcome == "won" else 2
        names = [n.strip() for n in form.getlist("player_name") if n.strip()]
        if not names:
            raise HTTPException(status_code=400, detail="Add at least one player")

        match = db.fetch_one(
            "insert into matches (game_id, session_id) values (%s, %s) returning id",
            [game["id"], session_id],
        )
        for name in names:
            player = find_or_create_player(name)
            db.execute(
                """
                insert into match_players (match_id, player_id, placement)
                values (%s, %s, %s)
                on conflict (match_id, player_id)
                do update set placement = excluded.placement
                """,
                [match["id"], player["id"], placement],
            )
        # No recompute: co-op games never enter the ratings table.
        return redirect(f"/leaderboard?game={game['id']}&saved={game['name']}")

    names = form.getlist("player_name")
    placements = form.getlist("placement")
    teams = form.getlist("team")

    rows: list[tuple[str, int, str | None]] = []
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        try:
            placement = int(placements[i])
        except (IndexError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"{name} needs a finishing position (1 = winner)",
            )
        if placement < 1:
            raise HTTPException(status_code=400, detail="Positions start at 1")
        team = (teams[i].strip() if i < len(teams) else "") or None
        rows.append((name, placement, team))

    if len(rows) < 2:
        raise HTTPException(status_code=400, detail="A result needs at least two players")

    # Mode-specific sanity checks — the same three modes the rating engine and
    # games.html know about. Nothing here is load-bearing for the maths (the
    # engine copes either way); it just stops obviously wrong results going in.
    if game["mode"] == "duel" and len(rows) != 2:
        raise HTTPException(
            status_code=400, detail="A head-to-head game has exactly two players"
        )
    if game["mode"] == "team":
        if any(team is None for _, _, team in rows):
            raise HTTPException(
                status_code=400, detail="Give every player a team name"
            )
        if len({team for _, _, team in rows}) < 2:
            raise HTTPException(
                status_code=400, detail="A team game needs at least two teams"
            )

    match = db.fetch_one(
        "insert into matches (game_id, session_id) values (%s, %s) returning id",
        [game["id"], session_id],
    )
    for name, placement, team in rows:
        player = find_or_create_player(name)
        db.execute(
            """
            insert into match_players (match_id, player_id, placement, team)
            values (%s, %s, %s, %s)
            on conflict (match_id, player_id)
            do update set placement = excluded.placement, team = excluded.team
            """,
            [match["id"], player["id"], placement, team],
        )

    # Replay the whole game so the new match and every rating it touches land
    # together and consistently.
    elo.recompute_game(game["id"])

    return redirect(f"/leaderboard?game={game['id']}&saved={game['name']}")


# ---------------------------------------------------------------------------
# Diagnostics — the two URLs to hit when something is broken
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Is the app itself running? Touches no database, so a 200 here plus a
    failing homepage tells you the problem is Neon, not FastAPI."""
    return {"status": "ok"}


@app.get("/db-check")
def db_check():
    """Can the app reach Neon? Returns the actual error text rather than a
    generic 500, so you can see what went wrong."""
    try:
        row = db.fetch_one("select now() as server_time")
        tables = db.fetch_all(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by table_name"
        )
        return {
            "connected": True,
            "server_time": row["server_time"],
            "tables": [t["table_name"] for t in tables],
        }
    except Exception as exc:
        return {"connected": False, "error": str(exc)}