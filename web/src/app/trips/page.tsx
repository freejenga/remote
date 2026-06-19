'use client';
import { useEffect, useState } from 'react';
import { getTrips, createTrip, quote, invoice, AuthError } from '@/lib/api';

const TYPES = ['Sedan', 'Wheelchair', 'Stretcher', 'Ambulatory'];

export default function TripsPage() {
  const [trips, setTrips] = useState<any[]>([]);
  const [form, setForm] = useState<any>({
    type: 'Sedan',
    roundtrip: false,
    subject: '',
    voucher: '',
    date: '',
    pickup: '',
    dropoff: '',
    miles: 0,
    wait: 0,
  });
  const [est, setEst] = useState<number | null>(null);
  const [msg, setMsg] = useState('');
  const [invSubject, setInvSubject] = useState('');
  const [inv, setInv] = useState<any>(null);

  async function load() {
    try {
      setTrips(await getTrips());
    } catch (e) {
      setMsg(e instanceof AuthError ? 'Please sign in to view trips.' : String(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  function upd(k: string, v: any) {
    setForm((f: any) => ({ ...f, [k]: v }));
  }

  async function getQuote() {
    try {
      setEst((await quote({ ...form, miles: +form.miles, wait: +form.wait })).amount);
    } catch (e) {
      setMsg(String(e));
    }
  }

  async function add(e: React.FormEvent) {
    e.preventDefault();
    try {
      await createTrip({ ...form, miles: +form.miles, wait: +form.wait });
      setMsg('Trip created.');
      setEst(null);
      load();
    } catch (e) {
      setMsg(e instanceof AuthError ? 'Please sign in.' : String(e));
    }
  }

  async function loadInvoice() {
    try {
      setInv(await invoice(invSubject.trim() || undefined));
    } catch (e) {
      setMsg(e instanceof AuthError ? 'Please sign in.' : String(e));
    }
  }

  return (
    <div>
      <h1>Transport dispatch</h1>
      <div className="card">
        <h3>New trip</h3>
        <form onSubmit={add}>
          <div className="row">
            <div>
              <label>Subject</label>
              <input
                value={form.subject}
                onChange={(e) => upd('subject', e.target.value)}
                placeholder="SUBJ-0012"
              />
            </div>
            <div>
              <label>Voucher</label>
              <input
                value={form.voucher}
                onChange={(e) => upd('voucher', e.target.value)}
              />
            </div>
          </div>
          <div className="row">
            <div>
              <label>Vehicle</label>
              <select value={form.type} onChange={(e) => upd('type', e.target.value)}>
                {TYPES.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </div>
            <div>
              <label>Round trip</label>
              <select
                value={String(form.roundtrip)}
                onChange={(e) => upd('roundtrip', e.target.value === 'true')}
              >
                <option value="false">One way</option>
                <option value="true">Round trip</option>
              </select>
            </div>
          </div>
          <div className="row">
            <div>
              <label>Miles</label>
              <input
                type="number"
                value={form.miles}
                onChange={(e) => upd('miles', e.target.value)}
              />
            </div>
            <div>
              <label>Wait (min)</label>
              <input
                type="number"
                value={form.wait}
                onChange={(e) => upd('wait', e.target.value)}
              />
            </div>
          </div>
          <div className="row">
            <div>
              <label>Pickup</label>
              <input
                value={form.pickup}
                onChange={(e) => upd('pickup', e.target.value)}
              />
            </div>
            <div>
              <label>Dropoff</label>
              <input
                value={form.dropoff}
                onChange={(e) => upd('dropoff', e.target.value)}
              />
            </div>
          </div>
          <label>Date</label>
          <input
            type="date"
            value={form.date}
            onChange={(e) => upd('date', e.target.value)}
          />
          <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
            <button
              type="button"
              className="btn"
              style={{ background: '#475569' }}
              onClick={getQuote}
            >
              Quote
            </button>
            <button className="btn">Create trip</button>
            {est != null && (
              <span>
                Estimate: <strong>${est.toFixed(2)}</strong>
              </span>
            )}
          </div>
          {msg && (
            <p className="muted" style={{ marginTop: 8 }}>
              {msg}
            </p>
          )}
        </form>
      </div>

      <div className="card">
        <h3>Trips ({trips.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Subject</th>
              <th>Date</th>
              <th>Vehicle</th>
              <th>Miles</th>
              <th>Amount</th>
            </tr>
          </thead>
          <tbody>
            {trips.map((t) => (
              <tr key={t.id}>
                <td>{t.subject}</td>
                <td>{t.date}</td>
                <td>{t.type}</td>
                <td>{Number(t.miles).toFixed(1)}</td>
                <td>${Number(t.amount).toFixed(2)}</td>
              </tr>
            ))}
            {trips.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No trips yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>Invoice</h3>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input
            value={invSubject}
            onChange={(e) => setInvSubject(e.target.value)}
            placeholder="Subject (blank = all)"
            style={{ maxWidth: 240 }}
          />
          <button type="button" className="btn" onClick={loadInvoice}>
            Generate
          </button>
        </div>
        {inv && (
          <p style={{ marginTop: 10 }}>
            {inv.subject ? (
              <>
                Subject <strong>{inv.subject}</strong> —{' '}
              </>
            ) : (
              <>All subjects — </>
            )}
            Trips: <strong>{inv.trip_count}</strong> · Miles:{' '}
            <strong>{inv.total_miles}</strong> · Total due:{' '}
            <strong>${Number(inv.total_due).toFixed(2)}</strong>
            {inv.service_period && inv.service_period !== '-' && (
              <span className="muted"> · {inv.service_period}</span>
            )}
          </p>
        )}
      </div>
    </div>
  );
}
