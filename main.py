"""
Non-Bored Game Nights.

Players, sessions, sign-ups.

Vercel finds this file automatically (it looks for main.py / index.py / app.py /
server.py) and finds the `app` variable inside it. There is no deployment config
to write.

Route map
---------
  GET  /                    what's next, who's coming
  GET  /players             everyone, and a form to add someone
  POST /players             create a player
  GET  /s/{slug}            THE WHATSAPP LINK — sign-up page for one game night
  POST /s/{slug}/signup     sign up (find-or-create player, record game votes)
  POST /s/{slug}/withdraw   drop out
  GET  /admin               create sessions and games (passcode required)
  GET  /health              is the app up (touches no database)
  GET  /db-check            can the app reach Neon
"""

import os
import secrets
import string
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import db

load_dotenv()  # Reads .env locally. On Vercel, env vars come from the dashboard.

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()

# Absolute path, not "templates". A serverless function does not always run with
# the working directory you expect; a relative path is the single most common
# way something works locally and breaks on deploy.
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ADMIN_COOKIE = "gn_admin"


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


def require_admin(request: Request) -> None:
    """Guards the admin pages. One shared passcode for the whole group.

    secrets.compare_digest rather than == so the comparison takes the same time
    whatever the input. Overkill here, but it is free and it is a good habit.
    """
    expected = os.environ.get("ADMIN_PASSCODE", "")
    supplied = request.cookies.get(ADMIN_COOKIE, "")
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})


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
    return db.fetch_all("select * from games where active order by lower(name)")


def find_or_create_player(name: str) -> dict:
    """The heart of frictionless sign-up: a typed name is enough.

    Matching is case-insensitive, so "dave" tonight and "Dave" next month are
    the same person. ON CONFLICT keeps it safe even if two people submit at the
    same instant.
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
        insert into players (name) values (%s)
        on conflict (lower(name)) do update set name = players.name
        returning *
        """,
        [name],
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
def players_page(request: Request, created: str | None = None):
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
        context={"players": people, "created": created},
    )


@app.post("/players")
def create_player(name: str = Form(...), emoji: str = Form("🎲")):
    player = find_or_create_player(name)
    if emoji.strip():
        db.execute(
            "update players set emoji = %s where id = %s",
            [emoji.strip()[:8], player["id"]],
        )
    return redirect(f"/players?created={player['name']}")


@app.get("/s/{slug}")
def session_page(request: Request, slug: str, joined: str | None = None):
    """This is the URL you paste into WhatsApp."""
    session = get_session_by_slug(slug)
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
        },
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
):
    when = datetime.fromisoformat(scheduled_for)
    slug = make_slug()
    db.execute(
        "insert into sessions (slug, title, scheduled_for, location) "
        "values (%s, %s, %s, %s)",
        [slug, title.strip(), when, location.strip() or None],
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
):
    db.execute(
        "insert into games (name, mode, min_players, max_players) "
        "values (%s, %s, %s, %s) on conflict do nothing",
        [name.strip(), mode, min_players, max_players],
    )
    return redirect("/admin")


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
