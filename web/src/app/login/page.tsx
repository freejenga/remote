'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { login } from '@/lib/api';

export default function LoginPage() {
  const router = useRouter();
  const [username, setU] = useState('');
  const [password, setP] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr('');
    try {
      await login(username, password);
      router.push('/');
    } catch (e: any) {
      setErr(e.message || 'Login failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 380, margin: '40px auto' }}>
      <h2>Sign in</h2>
      <form onSubmit={submit}>
        <label>Username</label>
        <input
          value={username}
          onChange={(e) => setU(e.target.value)}
          autoComplete="username"
        />
        <label>Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setP(e.target.value)}
          autoComplete="current-password"
        />
        <div style={{ marginTop: 14 }}>
          <button className="btn" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
        {err && <p style={{ color: '#dc2626', marginTop: 10 }}>{err}</p>}
      </form>
      <p className="muted" style={{ marginTop: 12 }}>
        Accounts are created by an admin. If auth is disabled on the server, you can use
        the modules without signing in.
      </p>
    </div>
  );
}
