"""
PostgreSQL adapter for PharmaTrack.

The query layer (queries.py, db.py) was written against SQLite's dialect:
'?' placeholders, datetime('now'), INSERT OR IGNORE, BEGIN IMMEDIATE and
LIKE are all SQLite idioms. Rather than fork the whole query layer, this
module exposes a connection that mirrors the sqlite3.Connection API
(cursor(), execute(), commit(), rollback(), close(), executescript()) and
translates those idioms to PostgreSQL when a statement is executed.

The desktop build and the test-suite keep using SQLite. Production (Render)
sets DATABASE_URL and the rest of the code is none the wiser.
"""

import re
from psycopg.rows import dict_row


def _translate(sql):
    """Turn one SQLite-flavoured statement into PostgreSQL."""
    if not sql:
        return sql

    # '?' positional placeholders -> psycopg '%s'.
    sql = sql.replace('?', '%s')

    # SQLite: datetime('now') -> e.g. 'YYYY-MM-DD HH:MM:SS'.
    # Postgres: to_char(now(), ...) gives the same TEXT format, so the
    # Python side keeps receiving strings identical in shape.
    sql = sql.replace(
        "datetime('now')",
        "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')",
    )

    # BEGIN IMMEDIATE is SQLite-only (acquires a write lock up front).
    # PostgreSQL handles locking transactionally, so it becomes a no-op;
    # the surrounding commit()/rollback() still govern the transaction.
    if re.fullmatch(r"\s*BEGIN IMMEDIATE\s*", sql):
        return "-- (BEGIN IMMEDIATE is a no-op on PostgreSQL)"

    # SQLite LIKE is case-insensitive for ASCII by default; PostgreSQL LIKE
    # is case-sensitive. ILIKE restores the intended search behaviour.
    sql = sql.replace(' LIKE ', ' ILIKE ')

    return sql


class _Cursor:
    """sqlite3.Cursor-compatible wrapper over a psycopg cursor."""

    def __init__(self, pg_cursor):
        self._pg = pg_cursor

    def execute(self, sql, params=()):
        pg_sql = _translate(sql)
        if pg_sql.startswith('--'):
            return self
        if params is None or params == ():
            self._pg.execute(pg_sql)
        else:
            self._pg.execute(pg_sql, params)
        return self

    def fetchone(self):
        return self._pg.fetchone()

    def fetchall(self):
        return self._pg.fetchall()


class PGConnection:
    """A sqlite3.Connection-compatible wrapper around psycopg.

    Only the surface used by queries.py / db.py is implemented:
    cursor(), execute(), commit(), rollback(), close(), executescript().
    """

    def __init__(self, conn):
        self._pg = conn

    def cursor(self):
        return _Cursor(self._pg.cursor(row_factory=dict_row))

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, script):
        # psycopg runs one statement at a time: strip every '--' comment
        # (full-line and trailing — a trailing comment can contain a ';'
        # that would otherwise fake a statement boundary), then split on ';'
        # like SQLite's executescript does (schema.sql contains no
        # procedural bodies or quoted semicolons).
        cleared = re.sub(r"--[^\n]*", "", script)
        for stmt in cleared.split(';'):
            if stmt.strip():
                self.execute(stmt, ())
        return self

    def commit(self):
        self._pg.commit()

    def rollback(self):
        self._pg.rollback()

    def close(self):
        self._pg.close()