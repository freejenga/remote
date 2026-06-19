"""Serengeti ride-dispatch module, incorporated into the clinical agent.

Ported from the Serengeti Inc. Dispatch Electron app (index.html). The pricing
engine is a pure function so it can be unit-tested and reused; trips are
persisted in the shared SQLite store. Trips key off `subject` / `voucher`,
which are the same identifiers used by the clinical protocol subjects and the
compliance module -- that shared key is the integration point.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .store import get_conn, pack, unpack
from . import audit

router = APIRouter(prefix='/dispatch', tags=['dispatch'])

# --- Pricing model (verbatim from the Serengeti app) -----------------------
RATES: Dict[str, Dict[str, float]] = {
    'Sedan': {'base': 15, 'perMile': 3.75, 'waitPerMin': 0.6},
    'Wheelchair': {'base': 20, 'perMile': 4.25, 'waitPerMin': 0.75},
    'Stretcher': {'base': 35, 'perMile': 5.0, 'waitPerMin': 1.0},
    'Ambulatory': {'base': 18, 'perMile': 4.0, 'waitPerMin': 0.65},
}
FEES = {'sanitation': 50, 'scheduling': 25}


def calculate_cost(miles: float, wait: float, vehicle_type: str, round_trip: bool) -> float:
    """Replicates calculateCost() from the dispatch UI."""
    rate = RATES.get(vehicle_type, RATES['Sedan'])
    trip_miles = miles * 2 if round_trip else miles
    total = (
        rate['base']
        + trip_miles * rate['perMile']
        + wait * rate['waitPerMin']
        + FEES['sanitation']
        + FEES['scheduling']
    )
    return round(total, 2)


# --- Schemas ---------------------------------------------------------------
class TripIn(BaseModel):
    type: str = 'Sedan'
    roundtrip: bool = False
    subject: str
    voucher: str
    date: Optional[str] = None
    pickup: Optional[str] = None
    dropoff: Optional[str] = None
    miles: float = 0.0
    wait: float = 0.0
    amount: Optional[float] = None  # auto-calculated when omitted

    @field_validator('subject')
    @classmethod
    def subject_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('subject must be a non-empty string')
        return v

    @field_validator('voucher')
    @classmethod
    def voucher_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('voucher must be a non-empty string')
        return v


class Trip(TripIn):
    id: str
    amount: float
    created_at: str = Field(alias='createdAt')

    model_config = {'populate_by_name': True}


def _row_to_trip(row) -> Trip:
    data = unpack(row['data'])
    return Trip(**data)


# --- Endpoints -------------------------------------------------------------
@router.get('/rates')
def get_rates():
    return {'rates': RATES, 'fees': FEES}


@router.post('/quote')
def quote(trip: TripIn):
    """Price a trip without saving it."""
    amount = calculate_cost(trip.miles, trip.wait, trip.type, trip.roundtrip)
    return {'amount': amount}


@router.get('/trips', response_model=List[Trip])
def list_trips():
    with get_conn() as conn:
        rows = conn.execute('SELECT data FROM trips ORDER BY created_at DESC').fetchall()
    return [_row_to_trip(r) for r in rows]


@router.post('/trips', response_model=Trip)
def create_trip(trip: TripIn):
    amount = trip.amount if trip.amount is not None else calculate_cost(
        trip.miles, trip.wait, trip.type, trip.roundtrip
    )
    record = Trip(
        id=f'{int(datetime.now().timestamp() * 1000)}-{uuid.uuid4().hex[:8]}',
        amount=amount,
        createdAt=datetime.now(timezone.utc).isoformat(),
        **trip.model_dump(exclude={'amount'}),
    )
    payload = record.model_dump(by_alias=True)
    with get_conn() as conn:
        conn.execute(
            'INSERT INTO trips (id, data, created_at) VALUES (?, ?, ?)',
            (record.id, pack(payload), record.created_at),
        )
    audit.record('dispatch.create_trip',
                 {'id': record.id, 'subject': record.subject, 'amount': record.amount})
    return record


@router.delete('/trips/{trip_id}')
def delete_trip(trip_id: str):
    with get_conn() as conn:
        cur = conn.execute('DELETE FROM trips WHERE id = ?', (trip_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail='Trip not found')
    audit.record('dispatch.delete_trip', {'id': trip_id})
    return {'success': True}


@router.get('/invoice')
def invoice(subject: Optional[str] = None):
    """Summarize trips into an invoice, optionally filtered by subject."""
    with get_conn() as conn:
        rows = conn.execute('SELECT data FROM trips ORDER BY created_at ASC').fetchall()
    trips = [_row_to_trip(r) for r in rows]
    if subject:
        trips = [t for t in trips if t.subject.lower() == subject.lower()]
    total = round(sum(t.amount for t in trips), 2)
    total_miles = round(sum(t.miles for t in trips), 1)
    dates = sorted(t.date for t in trips if t.date)
    return {
        'subject': subject,
        'trip_count': len(trips),
        'total_miles': total_miles,
        'total_due': total,
        'service_period': f'{dates[0]} to {dates[-1]}' if dates else '-',
        'trips': [t.model_dump(by_alias=True) for t in trips],
    }
