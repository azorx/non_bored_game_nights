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
def players_page(request: Request):
    """Read-only roster. Players come into existence by signing up for a night,
    so there is nothing to create here — this page just shows who exists."""
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
        context={"players": people},
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


@app.get("/s/{slug}")
def session_page(request: Request, slug: str, joined: str | None = None):
    """This is the URL you paste into WhatsApp."""
    return render_session(request, get_session_by_slug(slug), joined=joined)


@app.get("/games/{game_id}/image")
def game_image(game_id: str):
    """Serves a game's picture straight out of Postgres.

    Cached hard by the browser and by Vercel's CDN — the bytes never change for
    a given game unless you re-upload, and even then a fresh page load is enough
    to see it. This is what stops every sign-up page view from re-fetching a
    dozen images from the database.
    """
    row = db.fetch_one(
        "select image, image_type from games where id = %s", [game_id]
    )
    if not row or row["image"] is None:
        raise HTTPException(status_code=404, detail="No picture for that game")
    return Response(
        content=bytes(row["image"]),
        media_type=row["image_type"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
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
def admin_page(request: Request):
    sessions = db.fetch_all(
        """
        select s.*, count(su.id) as signup_count
        from sessions s
        left join signups su on su.session_id = s.id
        group by s.id
        order by s.scheduled_for desc
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"sessions": sessions, "games": active_games()},
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