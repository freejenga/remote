"""Tests for the minimal .env loader."""
import os
import tempfile

from app.envfile import load_dotenv


def _write(text):
    path = os.path.join(tempfile.gettempdir(), f'env_{os.getpid()}.env')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def test_loads_pairs_and_ignores_comments(monkeypatch):
    monkeypatch.delenv('FOO_TEST_KEY', raising=False)
    path = _write("# a comment\n\nFOO_TEST_KEY=hello\nBAD LINE NO EQUALS\n")
    applied = load_dotenv(path)
    assert applied.get('FOO_TEST_KEY') == 'hello'
    assert os.environ['FOO_TEST_KEY'] == 'hello'
    os.remove(path)


def test_does_not_override_existing_env(monkeypatch):
    monkeypatch.setenv('FOO_TEST_KEY', 'original')
    path = _write("FOO_TEST_KEY=changed\n")
    applied = load_dotenv(path)
    assert 'FOO_TEST_KEY' not in applied          # not applied
    assert os.environ['FOO_TEST_KEY'] == 'original'  # untouched
    os.remove(path)


def test_strips_quotes(monkeypatch):
    monkeypatch.delenv('QUOTED_KEY', raising=False)
    path = _write('QUOTED_KEY="sk-ant-abc123"\n')
    load_dotenv(path)
    assert os.environ['QUOTED_KEY'] == 'sk-ant-abc123'
    os.remove(path)


def test_missing_file_is_noop():
    assert load_dotenv('/no/such/.env') == {}
