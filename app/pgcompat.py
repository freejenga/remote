"""PostgreSQL compatibility shim for the shared store.

The platform is SQLite-first. This module is the *optional* Postgres backend
that plugs in behind ``store.get_conn`` when ``PLATFORM_DB_URL`` points at a
postgres server -- without rewriting the modules, which all use ``?`` placeholders
and read rows by both name and position (the way ``sqlite3.Row`` allows).

It provides exactly three things:

  * ``translate_sql`` -- rewrites SQLite SQL to the Postgres dialect: ``?`` ->
    ``%s`` placeholders, and ``INSERT OR REPLACE INTO t(cols)`` -> an
    ``ON CONFLICT`` upsert.
  * ``Row`` -- a hybrid row supporting ``row[0]`` *and* ``row['col']`` and
    ``dict(row)``, matching ``sqlite3.Row`` so calling code is unchanged.
  * ``connect`` -- a thin connection/cursor wrapper that applies the above.

NOTE: validated for SQL translation + row behaviour here; the live psycopg path
requires a running Postgres to exercise end-to-end.
"""
import re

_UPSERT_RE = re.compile(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I)


def _rewrite_upsert(sql: str) -> str:
    """Rewrite a SQLite ``INSERT OR REPLACE`` into a Postgres ``ON CONFLICT`` upsert.

    Assumes the first listed column is the conflict target (the platform's
    upserts key on the table's primary key, which is the first column).
    """
    m = _UPSERT_RE.search(sql)
    if not m:
        return sql
    cols = [c.strip() for c in m.group(2).split(",")]
    pk = cols[0]
    setters = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols[1:]) or f"{pk}=EXCLUDED.{pk}"
    base = _UPSERT_RE.sub(lambda mm: f"INSERT INTO {mm.group(1)} ({mm.group(2)})", sql, count=1)
    return f"{base} ON CONFLICT ({pk}) DO UPDATE SET {setters}"


def translate_sql(sql: str) -> str:
    """Translate SQLite SQL to Postgres: upsert rewrite, then ``?`` -> ``%s``."""
    return _rewrite_upsert(sql).replace("?", "%s")


class Row:
    """Hybrid row: positional and named access, plus ``dict(row)`` support."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = tuple(vals)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return list(self._cols)


def _row_factory(cursor):
    cols = [c.name for c in cursor.description] if cursor.description else []

    def make(values):
        return Row(cols, values)
    return make


class _Cursor:
    """Wraps a psycopg cursor, translating SQL on the way in."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(translate_sql(sql), params)
        return self._cur

    def executemany(self, sql, seq):
        self._cur.executemany(translate_sql(sql), list(seq))
        return self._cur

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Connection:
    """Minimal connection wrapper matching the subset of the sqlite3 API used."""
    dialect = "postgres"

    def __init__(self, url):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised only w/o psycopg
            raise RuntimeError(
                "PLATFORM_DB_URL points at Postgres but 'psycopg' is not installed. "
                "Install it: pip install -r requirements-postgres.txt") from exc
        self._conn = psycopg.connect(url, row_factory=_row_factory)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(translate_sql(sql), params)
        return cur

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(translate_sql(sql), list(seq))
        return cur

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def connect(url: str) -> Connection:
    return Connection(url)
