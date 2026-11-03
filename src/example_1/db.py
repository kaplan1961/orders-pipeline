"""PostgreSQL connection helpers and access to the packaged SQL files."""

from __future__ import annotations

from importlib.resources import files

import psycopg
from psycopg.rows import TupleRow

type Conn = psycopg.Connection[TupleRow]


def read_sql(name: str) -> str:
    """Read a SQL file shipped in the ``example_1/sql`` package folder.

    The files are trusted package resources (never user input); all runtime values are
    passed to psycopg as bound parameters, never interpolated into the SQL text.

    Args:
        name: File name, e.g. ``schema.sql``.

    Returns:
        The SQL text.
    """
    return files("example_1").joinpath("sql", name).read_text(encoding="utf-8")


def connect(database_url: str) -> Conn:
    """Open an autocommit connection; callers wrap work in explicit transactions.

    Args:
        database_url: PostgreSQL connection URL.

    Returns:
        An open psycopg connection.
    """
    return psycopg.connect(database_url, autocommit=True)


def apply_schema(conn: Conn) -> None:
    """Create the pipeline schemas and tables if they do not exist (idempotent).

    Args:
        conn: Open database connection.
    """
    conn.execute(read_sql("schema.sql"))
