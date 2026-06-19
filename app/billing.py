"""Financial reconciliation: Schedule-of-Events activities <-> study budget.

The financial counterpart to ``scheduling``: it maps each protocol *Study Event*
(a Schedule-of-Events activity) to a *Billing Item* in the study budget, then
reconciles a subject's actual visit history against that budget. Like the rest
of the deterministic core, **no LLM is involved** -- mapping is normalized-name
matching (with manual override) and the money math is plain arithmetic, so an
invoice or reconciliation report is reproducible and auditable.

Pipeline:

    budget (XLSX/CSV/JSON) --> budget_items
    SoE activities (soe_events) --map--> event_billing_map --> per-activity cost
    subject_visits (actuals) --reconcile--> completed / missed / pending $$$

The reconciliation output is shaped for direct export into accounting/billing
software (flat rows: subject, visit, activity, item, cost, status, date).
"""
import io
import os
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .store import get_conn
from . import audit, scheduling

router = APIRouter(prefix="/billing", tags=["billing"])

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(name: Optional[str]) -> str:
    """Normalize an activity/item name for matching (lower, depunctuated)."""
    s = (name or "").lower()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# Budget ingestion
# ---------------------------------------------------------------------------
def _coerce_cost(value) -> float:
    if value is None:
        return 0.0
    s = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return round(float(s), 2) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def rows_from_table(records: List[dict]) -> List[dict]:
    """Normalize arbitrary budget rows into ``{item_name, unit_cost, category}``.

    Column names are matched case-insensitively across common spellings so a
    sponsor's XLSX/CSV imports without manual remapping.
    """
    out = []
    for rec in records:
        lower = {str(k).strip().lower(): v for k, v in rec.items()}
        name = (lower.get("item_name") or lower.get("item") or lower.get("name")
                or lower.get("procedure") or lower.get("activity")
                or lower.get("description"))
        if not name or not str(name).strip():
            continue
        cost = (lower.get("unit_cost") or lower.get("cost") or lower.get("price")
                or lower.get("amount") or lower.get("rate"))
        category = lower.get("category") or lower.get("type") or lower.get("group")
        out.append({"item_name": str(name).strip(),
                    "unit_cost": _coerce_cost(cost),
                    "category": str(category).strip() if category else None})
    return out


def parse_budget_file(filename: str, content: bytes) -> List[dict]:
    """Parse an uploaded XLSX/CSV budget into normalized rows via pandas."""
    import pandas as pd
    lower = (filename or "").lower()
    try:
        if lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        elif lower.endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(content))
        else:
            raise ValueError("budget file must be .csv or .xlsx")
    except ValueError:
        raise
    except Exception as exc:  # malformed file
        raise ValueError(f"could not read budget file: {exc}") from exc
    return rows_from_table(df.to_dict(orient="records"))


def set_budget(study_id: str, items: List[dict]) -> dict:
    """Replace a study's budget with ``items`` ({item_name, unit_cost, category})."""
    rows = rows_from_table(items) if items and "item_name" not in items[0] else [
        {"item_name": i["item_name"], "unit_cost": _coerce_cost(i.get("unit_cost")),
         "category": i.get("category")}
        for i in items
    ]
    now = _now()
    records = [
        (f"bud_{uuid.uuid4().hex[:12]}", study_id, r["item_name"],
         normalize(r["item_name"]), r["unit_cost"], r.get("category"), now)
        for r in rows
    ]
    with get_conn() as conn:
        conn.execute("DELETE FROM budget_items WHERE study_id = ?", (study_id,))
        conn.executemany(
            "INSERT INTO budget_items (id, study_id, item_name, normalized_name, "
            "unit_cost, category, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", records)
    audit.record("billing.set_budget", {"study_id": study_id, "items": len(records)})
    return {"study_id": study_id, "item_count": len(records)}


def list_budget(study_id: str) -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, item_name, unit_cost, category FROM budget_items "
            "WHERE study_id = ? ORDER BY item_name", (study_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Event -> billing-item mapping
# ---------------------------------------------------------------------------
def _distinct_activities(conn, study_id: str) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT activity FROM soe_events WHERE study_id = ? AND activity IS NOT NULL",
        (study_id,)).fetchall()
    return [r["activity"] for r in rows]


def auto_map(study_id: str) -> dict:
    """Auto-match SoE activities to budget items by normalized name.

    Exact normalized match first, then a containment fallback. Existing *manual*
    mappings are preserved; only ``auto`` rows are recomputed.
    """
    now = _now()
    with get_conn() as conn:
        budget = conn.execute(
            "SELECT id, normalized_name FROM budget_items WHERE study_id = ?",
            (study_id,)).fetchall()
        manual = {r["normalized_activity"] for r in conn.execute(
            "SELECT normalized_activity FROM event_billing_map "
            "WHERE study_id = ? AND source = 'manual'", (study_id,)).fetchall()}
        activities = _distinct_activities(conn, study_id)

        by_norm = {b["normalized_name"]: b["id"] for b in budget}
        new_rows = []
        matched = 0
        for act in activities:
            nact = normalize(act)
            if nact in manual:
                continue
            item_id = by_norm.get(nact)
            if item_id is None:  # containment fallback
                for bnorm, bid in by_norm.items():
                    if bnorm and (bnorm in nact or nact in bnorm):
                        item_id = bid
                        break
            if item_id:
                matched += 1
            new_rows.append((f"ebm_{uuid.uuid4().hex[:12]}", study_id, act, nact,
                             item_id, "auto", now))
        conn.execute("DELETE FROM event_billing_map WHERE study_id = ? AND source = 'auto'",
                     (study_id,))
        conn.executemany(
            "INSERT INTO event_billing_map (id, study_id, activity, "
            "normalized_activity, budget_item_id, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", new_rows)
    audit.record("billing.auto_map",
                 {"study_id": study_id, "activities": len(new_rows), "matched": matched})
    return {"study_id": study_id, "activity_count": len(new_rows), "matched": matched}


def set_mapping(study_id: str, activity: str, budget_item_id: Optional[str]) -> dict:
    """Manually map (or unmap, with ``budget_item_id=None``) an activity."""
    nact = normalize(activity)
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM event_billing_map WHERE study_id = ? AND normalized_activity = ?",
            (study_id, nact))
        conn.execute(
            "INSERT INTO event_billing_map (id, study_id, activity, "
            "normalized_activity, budget_item_id, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'manual', ?)",
            (f"ebm_{uuid.uuid4().hex[:12]}", study_id, activity, nact,
             budget_item_id, now))
    audit.record("billing.set_mapping",
                 {"study_id": study_id, "activity": activity, "item": budget_item_id})
    return {"study_id": study_id, "activity": activity, "budget_item_id": budget_item_id}


def get_mappings(study_id: str) -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.activity, m.source, b.item_name, b.unit_cost "
            "FROM event_billing_map m LEFT JOIN budget_items b ON b.id = m.budget_item_id "
            "WHERE m.study_id = ? ORDER BY m.activity", (study_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
_BILLING_STATUS = {"on-time": "completed", "early": "completed", "late": "completed"}


def _billing_status(visit: dict, today: Optional[str]) -> str:
    """Map a subject visit to a billing status (completed/missed/pending)."""
    sched = scheduling.compliance_status(visit, actual=visit.get("actual_date"),
                                         today=today)
    if visit.get("actual_date"):
        return "completed"
    if sched == "missed":
        return "missed"
    return "pending"


def reconcile(study_id: str, today: Optional[str] = None) -> dict:
    """Reconcile subject visits against the budget. Returns summary + line items.

    Each line item is one (subject, visit, activity) with its mapped billing
    item, unit cost, and billing status. Totals roll up by status so completed
    work is invoiceable, missed work is quantified as lost revenue, and pending
    work is the forecast.
    """
    today = today or scheduling._today_default()
    with get_conn() as conn:
        visits = conn.execute(
            "SELECT id, subject_id, visit_name, target_date, earliest_date, "
            "latest_date, actual_date FROM subject_visits WHERE study_id = ?",
            (study_id,)).fetchall()
        # activity -> (item_name, unit_cost) for the study.
        cost_rows = conn.execute(
            "SELECT m.normalized_activity, b.item_name, b.unit_cost "
            "FROM event_billing_map m JOIN budget_items b ON b.id = m.budget_item_id "
            "WHERE m.study_id = ?", (study_id,)).fetchall()
        cost_by_act = {r["normalized_activity"]: (r["item_name"], r["unit_cost"])
                       for r in cost_rows}
        # visit_name -> [activities]
        soe = conn.execute(
            "SELECT visit_name, activity FROM soe_events WHERE study_id = ?",
            (study_id,)).fetchall()
    acts_by_visit: dict = {}
    for r in soe:
        acts_by_visit.setdefault(r["visit_name"], []).append(r["activity"])

    line_items = []
    summary = {"completed": {"count": 0, "amount": 0.0},
               "missed": {"count": 0, "amount": 0.0},
               "pending": {"count": 0, "amount": 0.0}}
    for v in visits:
        vd = dict(v)
        status = _billing_status(vd, today)
        for activity in acts_by_visit.get(vd["visit_name"], []):
            item_name, unit_cost = cost_by_act.get(normalize(activity), (None, 0.0))
            unit_cost = round(unit_cost or 0.0, 2)
            line_items.append({
                "subject_id": vd["subject_id"], "visit_name": vd["visit_name"],
                "activity": activity, "billing_item": item_name,
                "unit_cost": unit_cost, "status": status,
                "date": vd.get("actual_date") or vd.get("target_date"),
                "billable": status == "completed" and item_name is not None,
            })
            summary[status]["count"] += 1
            summary[status]["amount"] = round(summary[status]["amount"] + unit_cost, 2)
    return {"study_id": study_id, "today": today, "summary": summary,
            "line_item_count": len(line_items), "line_items": line_items}


def export_reconciliation(study_id: str, fmt: str = "xlsx",
                          output_dir: str = "outputs") -> str:
    """Write the reconciliation as a flat, accounting-ready CSV/XLSX file."""
    import pandas as pd
    report = reconcile(study_id)
    os.makedirs(output_dir, exist_ok=True)
    df = pd.DataFrame(report["line_items"], columns=[
        "subject_id", "visit_name", "activity", "billing_item", "unit_cost",
        "status", "date", "billable"])
    if fmt.lower() == "csv":
        path = os.path.join(output_dir, f"reconciliation_{uuid.uuid4().hex[:8]}.csv")
        df.to_csv(path, index=False)
    else:
        path = os.path.join(output_dir, f"reconciliation_{uuid.uuid4().hex[:8]}.xlsx")
        summary = report["summary"]
        sum_df = pd.DataFrame([
            {"status": k, "count": v["count"], "amount": v["amount"]}
            for k, v in summary.items()])
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="line_items", index=False)
            sum_df.to_excel(writer, sheet_name="summary", index=False)
    audit.record("billing.export", {"study_id": study_id, "fmt": fmt})
    return path


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class BudgetIn(BaseModel):
    items: List[dict]


class MappingIn(BaseModel):
    activity: str
    budget_item_id: Optional[str] = None


@router.post("/studies/{study_id}/budget")
def post_budget(study_id: str, body: BudgetIn):
    return set_budget(study_id, body.items)


@router.post("/studies/{study_id}/budget/upload")
async def upload_budget(study_id: str, file: UploadFile = File(...)):
    try:
        rows = parse_budget_file(file.filename, await file.read())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return set_budget(study_id, rows)


@router.get("/studies/{study_id}/budget")
def get_budget(study_id: str):
    return {"study_id": study_id, "items": list_budget(study_id)}


@router.post("/studies/{study_id}/map/auto")
def post_auto_map(study_id: str):
    return auto_map(study_id)


@router.put("/studies/{study_id}/map")
def put_mapping(study_id: str, body: MappingIn):
    return set_mapping(study_id, body.activity, body.budget_item_id)


@router.get("/studies/{study_id}/map")
def get_map(study_id: str):
    return {"study_id": study_id, "mappings": get_mappings(study_id)}


@router.get("/studies/{study_id}/reconcile")
def get_reconcile(study_id: str, today: Optional[str] = None):
    return reconcile(study_id, today)


@router.get("/studies/{study_id}/reconcile/export")
def get_reconcile_export(study_id: str, fmt: str = "xlsx"):
    path = export_reconciliation(study_id, fmt)
    media = ("text/csv" if fmt.lower() == "csv"
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return FileResponse(path=path, filename=os.path.basename(path), media_type=media)
