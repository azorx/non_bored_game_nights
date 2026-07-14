"""
Database access. Deliberately thin: no ORM, just parameterised SQL.

You know SQL, so an ORM would only stand between you and the thing you already
understand. The three helpers below are all you need for the whole project.

One serverless-specific note: a Vercel function may be frozen and thawed between
requests, so a long-lived connection pool held in a module-level variable can
end up holding sockets that died while the function was asleep. Opening a fresh
connection per request is slightly slower but always correct, and at game-night
traffic levels the difference is unmeasurable. Neon's *pooled* connection string
(the hostname with `-pooler` in it) is what makes this cheap — use that one.
"""

import os
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row


class MissingDatabaseUrl(RuntimeError):
    pass


def _connection_string() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise MissingDatabaseUrl(
            "DATABASE_URL is not set. Locally, put it in .env . "
            "On Vercel, set it under Settings -> Environment Variables."
        )
    return url


def connect() -> psycopg.Connection:
    """Open a connection that returns rows as dictionaries rather than tuples."""
    return psycopg.connect(_connection_string(), row_factory=dict_row)


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Run a query, return every row as a dict."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    """Run a query, return the first row as a dict (or None)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Sequence[Any] | None = None) -> None:
    """Run an INSERT / UPDATE / DELETE. Commits on success, rolls back on error.

    Always pass values via `params`, never by formatting them into `sql` —
    that is what stops a player called `Robert'); drop table hello;--` from
    ruining your evening.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()
