"""Tests for the optional Postgres adapter's dialect translation + row shim.

These validate the SQL translation and the hybrid Row without needing a live
Postgres server (the psycopg connection path requires a running database).
"""
import os
import tempfile

os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'pgcompat_test.db')

from app import pgcompat
from app import store


def test_placeholder_translation():
    assert pgcompat.translate_sql('SELECT * FROM t WHERE id = ?') == \
        'SELECT * FROM t WHERE id = %s'
    assert pgcompat.translate_sql(
        'INSERT INTO t (a, b) VALUES (?, ?)') == \
        'INSERT INTO t (a, b) VALUES (%s, %s)'


def test_insert_or_replace_becomes_upsert():
    out = pgcompat.translate_sql(
        "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)")
    assert out.startswith("INSERT INTO app_state (key, value) VALUES (%s, %s)")
    assert "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value" in out


def test_hybrid_row_positional_and_named():
    row = pgcompat.Row(['id', 'name'], ['x1', 'Vitals'])
    assert row[0] == 'x1' and row[1] == 'Vitals'
    assert row['id'] == 'x1' and row['name'] == 'Vitals'
    # dict(row) and tuple-iteration both work (sqlite3.Row parity).
    assert dict(row) == {'id': 'x1', 'name': 'Vitals'}
    assert list(row) == ['x1', 'Vitals']
    assert len(row) == 2


def test_dict_of_rows_like_studies_count_query():
    rows = [pgcompat.Row(['study_id', 'n'], ['s1', 3]),
            pgcompat.Row(['study_id', 'n'], ['s2', 5])]
    assert dict(rows) == {'s1': 3, 's2': 5}


def test_current_dialect_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv('PLATFORM_DB_URL', raising=False)
    assert store.current_dialect() == 'sqlite'


def test_current_dialect_detects_postgres(monkeypatch):
    monkeypatch.setenv('PLATFORM_DB_URL', 'postgresql://u:p@localhost/db')
    assert store.current_dialect() == 'postgres'
    monkeypatch.setenv('PLATFORM_DB_URL', 'postgres://u:p@localhost/db')
    assert store.current_dialect() == 'postgres'
