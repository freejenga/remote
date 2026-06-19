'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { getSubjects, saveSubjects, integrity, AuthError } from '@/lib/api';

export default function SubjectsPage() {
  const [subjects, setSubjects] = useState<any[]>([]);
  const [sid, setSid] = useState('');
  const [status, setStatus] = useState('enrolled');
  const [med, setMed] = useState('');
  const [msg, setMsg] = useState('');
  const [ig, setIg] = useState<any>(null);

  async function load() {
    try {
      setSubjects(await getSubjects());
      setIg(await integrity());
    } catch (e) {
      setMsg(e instanceof AuthError ? 'Please sign in to view subjects.' : String(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!sid.trim()) return;
    const next = [
      ...subjects.filter((s) => s.subjectId !== sid),
      { subjectId: sid, status, med },
    ];
    try {
      await saveSubjects(next);
      setSid('');
      setMed('');
      setMsg('Saved.');
      load();
    } catch (e) {
      setMsg(e instanceof AuthError ? 'Please sign in.' : String(e));
    }
  }

  return (
    <div>
      <h1>Subjects &amp; compliance</h1>
      <div className="card">
        <h3>Add / update subject</h3>
        <form onSubmit={add}>
          <div className="row">
            <div>
              <label>Subject ID</label>
              <input
                value={sid}
                onChange={(e) => setSid(e.target.value)}
                placeholder="SUBJ-0012"
              />
            </div>
            <div>
              <label>Status</label>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option>enrolled</option>
                <option>screening</option>
                <option>withdrawn</option>
                <option>completed</option>
              </select>
            </div>
          </div>
          <label>Medication</label>
          <input
            value={med}
            onChange={(e) => setMed(e.target.value)}
            placeholder="e.g. Sertraline 50 mg"
          />
          <div style={{ marginTop: 12 }}>
            <button className="btn">Save</button>
          </div>
          {msg && (
            <p className="muted" style={{ marginTop: 8 }}>
              {msg}
            </p>
          )}
        </form>
      </div>

      <div className="card">
        <h3>Subjects ({subjects.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Subject ID</th>
              <th>Status</th>
              <th>Medication</th>
            </tr>
          </thead>
          <tbody>
            {subjects.map((s) => (
              <tr key={s.subjectId}>
                <td>
                  <Link href={`/subjects/${encodeURIComponent(s.subjectId)}`}>
                    {s.subjectId}
                  </Link>
                </td>
                <td>{s.status}</td>
                <td>{s.med || '—'}</td>
              </tr>
            ))}
            {subjects.length === 0 && (
              <tr>
                <td colSpan={3} className="muted">
                  No subjects yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {ig && (
        <div className="card">
          <h3>Integrity</h3>
          <p>
            Status:{' '}
            <span className={'badge ' + (ig.ok ? 'ok' : 'warn')}>
              {ig.ok ? 'clean' : 'orphans found'}
            </span>
          </p>
          {!ig.ok && (
            <p className="muted">
              Orphan trips: {ig.orphan_trips?.length || 0}, orphan visit logs:{' '}
              {ig.orphan_visit_logs?.length || 0}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
