"""Tests for deterministic visit scheduling, windows, and compliance."""
import os
import tempfile

# Isolated throwaway DB before any app import.
os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'scheduling_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

from fastapi.testclient import TestClient

from app.api import app
from app import scheduling

client = TestClient(app)

SAMPLE = open(
    os.path.join(os.path.dirname(__file__), '..', 'sample_protocol.txt'),
    encoding='utf-8',
).read()


# --- pure algorithms -------------------------------------------------------
def test_parse_window_formats():
    assert scheduling.parse_window('±3 days') == (3, 3)
    assert scheduling.parse_window('+/- 2') == (2, 2)
    assert scheduling.parse_window('+7 days') == (0, 7)
    assert scheduling.parse_window('-2 days') == (2, 0)
    assert scheduling.parse_window('+3/-2') == (2, 3)
    assert scheduling.parse_window('-2 to +3') == (2, 3)
    assert scheduling.parse_window('±0') == (0, 0)
    assert scheduling.parse_window(None) == (0, 0)
    assert scheduling.parse_window('n/a') == (0, 0)


def test_visit_window_anchored_on_day_one():
    # Day 1 == enrollment; Day -7 is seven days earlier.
    w1 = scheduling.visit_window('2026-06-15', 1, '±0')
    assert w1['target_date'] == '2026-06-15'
    assert w1['earliest_date'] == '2026-06-15' and w1['latest_date'] == '2026-06-15'

    w2 = scheduling.visit_window('2026-06-15', -6, '±3 days')
    assert w2['target_date'] == '2026-06-08'      # 15 + (-6 - 1) = 8
    assert w2['earliest_date'] == '2026-06-05'
    assert w2['latest_date'] == '2026-06-11'


def test_visit_window_unknown_day_is_none():
    w = scheduling.visit_window('2026-06-15', None, '±3')
    assert w == {'target_date': None, 'earliest_date': None, 'latest_date': None}


def test_compliance_status_with_actual():
    w = scheduling.visit_window('2026-06-15', 8, '±3 days')  # target 06-22, window 06-19..06-25
    assert scheduling.compliance_status(w, actual='2026-06-22') == 'on-time'
    assert scheduling.compliance_status(w, actual='2026-06-19') == 'on-time'  # earliest boundary
    assert scheduling.compliance_status(w, actual='2026-06-18') == 'early'    # before window
    assert scheduling.compliance_status(w, actual='2026-06-26') == 'late'     # after window


def test_compliance_status_pending_relative_to_today():
    w = scheduling.visit_window('2026-06-15', 8, '±3 days')  # window 06-19..06-25
    assert scheduling.compliance_status(w, today='2026-06-10') == 'upcoming'
    assert scheduling.compliance_status(w, today='2026-06-22') == 'due'
    assert scheduling.compliance_status(w, today='2026-07-01') == 'missed'


def test_next_due_picks_soonest_pending():
    visits = [
        scheduling.visit_window('2026-06-15', 1, '±0') | {'actual_date': '2026-06-15'},
        scheduling.visit_window('2026-06-15', 8, '±3') | {'actual_date': None},
        scheduling.visit_window('2026-06-15', 22, '±3') | {'actual_date': None},
    ]
    nxt = scheduling.next_due(visits, today='2026-06-16')
    assert nxt is not None
    assert nxt['target_date'] == '2026-06-22'   # day 8, the soonest pending
    assert nxt['status'] in ('upcoming', 'due')


def test_next_due_none_when_all_done_or_missed():
    visits = [
        scheduling.visit_window('2026-06-15', 1, '±0') | {'actual_date': '2026-06-15'},
        scheduling.visit_window('2026-06-15', 8, '±3') | {'actual_date': None},
    ]
    # Far in the future -> the only pending visit is now missed, not due.
    assert scheduling.next_due(visits, today='2027-01-01') is None


def test_determinism():
    a = scheduling.visit_window('2026-01-01', 15, '±2')
    b = scheduling.visit_window('2026-01-01', 15, '±2')
    assert a == b


# --- end-to-end through the API -------------------------------------------
def _study_with_version():
    sid = client.post('/studies', json={'name': 'SCHED-1'}).json()['id']
    vid = client.post(f'/studies/{sid}/versions',
                      json={'label': 'v1', 'protocol_text': SAMPLE}).json()['id']
    return sid, vid


def test_soe_materialize_and_calendar_flow():
    sid, vid = _study_with_version()

    # Materialize the Schedule of Events from the parsed version.
    r = client.post(f'/scheduling/studies/{sid}/soe', params={'version_id': vid})
    assert r.status_code == 200 and r.json()['event_count'] > 0

    soe = client.get(f'/scheduling/studies/{sid}/soe').json()
    assert soe['events'], 'expected materialized SoE events'

    # Generate a subject's visit calendar anchored on enrollment.
    cal = client.post('/scheduling/subjects/SUBJ-1/calendar',
                      json={'study_id': sid, 'enrollment_date': '2026-06-15'})
    assert cal.status_code == 200 and cal.json()['visit_count'] > 0

    # Compliance report lists each visit with a status.
    rep = client.get('/scheduling/compliance',
                     params={'study_id': sid, 'today': '2026-06-15'}).json()
    assert rep['total'] == cal.json()['visit_count']
    assert rep['visits'] and 'status' in rep['visits'][0]

    # Record an actual on the first visit, then confirm it reads back.
    vid_first = rep['visits'][0]['id']
    pa = client.patch(f'/scheduling/visits/{vid_first}/actual',
                      json={'actual_date': '2026-06-15'})
    assert pa.status_code == 200
    rep2 = client.get('/scheduling/compliance',
                      params={'study_id': sid, 'today': '2026-06-15'}).json()
    done = [v for v in rep2['visits'] if v['id'] == vid_first][0]
    assert done['actual_date'] == '2026-06-15'
    assert done['status'] in ('on-time', 'early', 'late')


def test_next_due_endpoint():
    sid, vid = _study_with_version()
    client.post(f'/scheduling/studies/{sid}/soe', params={'version_id': vid})
    client.post('/scheduling/subjects/SUBJ-ND/calendar',
                json={'study_id': sid, 'enrollment_date': '2026-06-15'})
    nd = client.get('/scheduling/next-due',
                    params={'study_id': sid, 'today': '2026-06-01'}).json()
    assert 'next_overall' in nd


def test_calendar_rejects_bad_date():
    sid, vid = _study_with_version()
    client.post(f'/scheduling/studies/{sid}/soe', params={'version_id': vid})
    r = client.post('/scheduling/subjects/SUBJ-X/calendar',
                    json={'study_id': sid, 'enrollment_date': 'June 1st'})
    assert r.status_code == 400


def test_scheduling_tables_exist():
    from app.store import get_conn
    with get_conn() as conn:
        names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {'soe_events', 'subject_visits'} <= names
