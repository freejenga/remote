'use client';
import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { getSubjects, getVisitLogs, getTrips, invoice, AuthError } from '@/lib/api';

// Cross-module subject view: composes compliance + visit logs + trips + invoice
// by the shared subject id — the platform's integration point, made visual.
export default function SubjectDetail() {
  const { id } = useParams<{ id: string }>();
  const sid = decodeURIComponent(id);
  const [subject, setSubject] = useState<any>(null);
  const [logs, setLogs] = useState<any[]>([]);
  const [trips, setTrips] = useState<any[]>([]);
  const [inv, setInv] = useState<any>(null);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    (async () => {
      try {
        const subs = await getSubjects();
        setSubject(subs.find((s: any) => s.subjectId === sid) || null);
        const allLogs = await getVisitLogs();
        setLogs(allLogs.filter((l: any) => (l.subjectId || l.subject) === sid));
        const allTrips = await getTrips();
        setTrips(allTrips.filter((t: any) => t.subject === sid));
        setInv(await invoice(sid));
      } catch (e) {
        setMsg(
          e instanceof AuthError ? 'Please sign in to view this subject.' : String(e)
        );
      }
    })();
  }, [sid]);

  return (
    <div>
      <p className="muted">
        <Link href="/subjects">← Subjects</Link>
      </p>
      <h1>{sid}</h1>
      {msg && (
        <div className="card">
          <p className="muted">{msg}</p>
        </div>
      )}

      <div className="card">
        <h3>Compliance</h3>
        {subject ? (
          <p>
            Status: <span className="badge ok">{subject.status || '—'}</span>
            {subject.med ? (
              <>
                {' '}
                · Medication: <strong>{subject.med}</strong>
              </>
            ) : null}
          </p>
        ) : (
          <p className="muted">No compliance record for this subject.</p>
        )}
      </div>

      <div className="card">
        <h3>Visit logs ({logs.length})</h3>
        {logs.length ? (
          <table>
            <thead>
              <tr>
                <th>Visit</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l, i) => (
                <tr key={i}>
                  <td>{l.visit || '—'}</td>
                  <td>{l.status || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">None.</p>
        )}
      </div>

      <div className="card">
        <h3>Transport trips ({trips.length})</h3>
        {trips.length ? (
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Vehicle</th>
                <th>Miles</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>
              {trips.map((t) => (
                <tr key={t.id}>
                  <td>{t.date}</td>
                  <td>{t.type}</td>
                  <td>{Number(t.miles).toFixed(1)}</td>
                  <td>${Number(t.amount).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">None.</p>
        )}
      </div>

      {inv && (
        <div className="card">
          <h3>Invoice summary</h3>
          <p>
            Trips: <strong>{inv.trip_count}</strong> · Miles:{' '}
            <strong>{inv.total_miles}</strong> · Total due:{' '}
            <strong>${Number(inv.total_due).toFixed(2)}</strong>
          </p>
          {inv.service_period && inv.service_period !== '-' && (
            <p className="muted">Service period: {inv.service_period}</p>
          )}
        </div>
      )}
    </div>
  );
}
