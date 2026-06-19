"""Tests for financial reconciliation (SoE activities <-> budget items)."""
import os
import tempfile

os.environ['PLATFORM_DB'] = os.path.join(tempfile.gettempdir(), 'billing_test.db')
if os.path.exists(os.environ['PLATFORM_DB']):
    os.remove(os.environ['PLATFORM_DB'])

from fastapi.testclient import TestClient

from app.api import app
from app import billing

client = TestClient(app)

SAMPLE = open(
    os.path.join(os.path.dirname(__file__), '..', 'sample_protocol.txt'),
    encoding='utf-8',
).read()


# --- pure helpers ----------------------------------------------------------
def test_normalize():
    assert billing.normalize("  ECG (12-lead)! ") == "ecg 12 lead"
    assert billing.normalize(None) == ""


def test_rows_from_table_flexible_columns():
    rows = billing.rows_from_table([
        {"Procedure": "Vitals", "Cost": "$120.50", "Type": "safety"},
        {"name": "ECG", "price": 300},
        {"foo": "bar"},  # no name -> skipped
    ])
    assert len(rows) == 2
    assert rows[0] == {"item_name": "Vitals", "unit_cost": 120.5, "category": "safety"}
    assert rows[1]["unit_cost"] == 300.0


# --- end-to-end through the API -------------------------------------------
def _study_with_soe_and_calendar():
    sid = client.post('/studies', json={'name': 'BILL-1'}).json()['id']
    vid = client.post(f'/studies/{sid}/versions',
                      json={'label': 'v1', 'protocol_text': SAMPLE}).json()['id']
    client.post(f'/scheduling/studies/{sid}/soe', params={'version_id': vid})
    client.post('/scheduling/subjects/SUBJ-1/calendar',
                json={'study_id': sid, 'enrollment_date': '2026-06-15'})
    return sid, vid


def _soe_activities(sid):
    return [e['activity'] for e in client.get(
        f'/scheduling/studies/{sid}/soe').json()['events'] if e['activity']]


def test_budget_set_and_list():
    sid, _ = _study_with_soe_and_calendar()
    r = client.post(f'/billing/studies/{sid}/budget',
                    json={'items': [{'item_name': 'Vitals', 'unit_cost': 100},
                                    {'item_name': 'ECG', 'unit_cost': 250}]})
    assert r.status_code == 200 and r.json()['item_count'] == 2
    items = client.get(f'/billing/studies/{sid}/budget').json()['items']
    assert {i['item_name'] for i in items} == {'Vitals', 'ECG'}


def test_auto_map_matches_activities_to_budget():
    sid, _ = _study_with_soe_and_calendar()
    acts = _soe_activities(sid)
    assert acts, 'expected SoE activities from the sample protocol'
    # Build a budget whose item names equal the first two activities.
    client.post(f'/billing/studies/{sid}/budget',
                json={'items': [{'item_name': acts[0], 'unit_cost': 100},
                                {'item_name': acts[1] if len(acts) > 1 else acts[0],
                                 'unit_cost': 250}]})
    r = client.post(f'/billing/studies/{sid}/map/auto')
    assert r.status_code == 200
    assert r.json()['matched'] >= 1
    mappings = client.get(f'/billing/studies/{sid}/map').json()['mappings']
    assert any(m['item_name'] for m in mappings)


def test_manual_override_mapping():
    sid, _ = _study_with_soe_and_calendar()
    acts = _soe_activities(sid)
    client.post(f'/billing/studies/{sid}/budget',
                json={'items': [{'item_name': 'Custom Item', 'unit_cost': 500}]})
    item_id = client.get(f'/billing/studies/{sid}/budget').json()['items'][0]['id']
    r = client.put(f'/billing/studies/{sid}/map',
                   json={'activity': acts[0], 'budget_item_id': item_id})
    assert r.status_code == 200
    # auto-map must not clobber the manual mapping.
    client.post(f'/billing/studies/{sid}/map/auto')
    mappings = client.get(f'/billing/studies/{sid}/map').json()['mappings']
    manual = [m for m in mappings if m['source'] == 'manual']
    assert manual and manual[0]['item_name'] == 'Custom Item'


def test_reconcile_completed_missed_pending():
    sid, _ = _study_with_soe_and_calendar()
    acts = _soe_activities(sid)
    client.post(f'/billing/studies/{sid}/budget',
                json={'items': [{'item_name': a, 'unit_cost': 100} for a in set(acts)]})
    client.post(f'/billing/studies/{sid}/map/auto')

    # Mark the first visit completed by recording an actual date.
    rep = client.get('/scheduling/compliance', params={'study_id': sid,
                                                       'today': '2026-06-15'}).json()
    first_visit_id = rep['visits'][0]['id']
    client.patch(f'/scheduling/visits/{first_visit_id}/actual',
                 json={'actual_date': '2026-06-15'})

    # Reconcile far in the future so unrecorded visits count as missed.
    r = client.get(f'/billing/studies/{sid}/reconcile', params={'today': '2027-01-01'})
    assert r.status_code == 200
    body = r.json()
    assert body['line_item_count'] > 0
    summary = body['summary']
    assert summary['completed']['count'] >= 1
    assert summary['completed']['amount'] > 0
    assert summary['missed']['count'] >= 1
    # Every line item carries the accounting-ready fields.
    li = body['line_items'][0]
    assert set(li) >= {'subject_id', 'visit_name', 'activity', 'billing_item',
                       'unit_cost', 'status', 'date', 'billable'}


def test_reconcile_export_xlsx_and_csv():
    sid, _ = _study_with_soe_and_calendar()
    acts = _soe_activities(sid)
    client.post(f'/billing/studies/{sid}/budget',
                json={'items': [{'item_name': a, 'unit_cost': 50} for a in set(acts)]})
    client.post(f'/billing/studies/{sid}/map/auto')

    for fmt, ctype in [('xlsx', 'spreadsheetml'), ('csv', 'text/csv')]:
        r = client.get(f'/billing/studies/{sid}/reconcile/export', params={'fmt': fmt})
        assert r.status_code == 200
        assert ctype in r.headers['content-type']


def test_budget_upload_csv():
    sid, _ = _study_with_soe_and_calendar()
    csv = b"item_name,unit_cost,category\nVitals,100,safety\nECG,250,cardiac\n"
    r = client.post(f'/billing/studies/{sid}/budget/upload',
                    files={'file': ('budget.csv', csv, 'text/csv')})
    assert r.status_code == 200 and r.json()['item_count'] == 2


def test_billing_tables_exist():
    from app.store import get_conn
    with get_conn() as conn:
        names = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {'budget_items', 'event_billing_map'} <= names
