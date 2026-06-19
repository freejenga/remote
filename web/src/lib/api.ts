// Typed client for the Clinical Research Platform FastAPI backend.
// All calls go through the same-origin /api proxy (see next.config.mjs) so the
// HttpOnly session cookie is sent automatically.
const BASE = process.env.NEXT_PUBLIC_API_BASE || '/api';

async function jget(path: string) {
  const res = await fetch(BASE + path, { credentials: 'include' });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

async function jpost(path: string, body: any) {
  const res = await fetch(BASE + path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export class AuthError extends Error {
  constructor() {
    super('Not authenticated');
    this.name = 'AuthError';
  }
}

// --- Auth / RBAC ---
export async function login(username: string, password: string) {
  const res = await fetch(BASE + '/rbac/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username, password }),
  });
  if (!res.ok) throw new Error('Invalid username or password');
  return res.json();
}
export async function logout() {
  await fetch(BASE + '/rbac/logout', { method: 'POST', credentials: 'include' });
}
export async function me() {
  try {
    return await jget('/rbac/me');
  } catch {
    return null;
  }
}
export const security = () => jget('/');

// --- Compliance ---
export const getSubjects = () =>
  jget('/compliance/saved_subjects').then((d) => d.data || []);
export const saveSubjects = (subjects: any[]) =>
  jpost('/compliance/saved_subjects', subjects);
export const getVisitLogs = () =>
  jget('/compliance/visit_logs').then((d) => d.data || []);
export const integrity = () => jget('/compliance/integrity');

// --- Dispatch ---
export const getTrips = () => jget('/dispatch/trips');
export const quote = (trip: any) => jpost('/dispatch/quote', trip);
export const createTrip = (trip: any) => jpost('/dispatch/trips', trip);
export const invoice = (subject?: string) =>
  jget(
    '/dispatch/invoice' + (subject ? `?subject=${encodeURIComponent(subject)}` : '')
  );

// --- Protocol ---
export async function parseText(protocolText: string) {
  const res = await fetch(BASE + '/parse-text', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ protocol_text: protocolText }),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// --- Audit ---
export const auditVerify = () => jget('/audit/verify');
export const auditEntries = (limit = 50) => jget(`/audit/entries?limit=${limit}`);

// --- Chat ---
export const chat = (messages: any[], subject?: string) =>
  jpost('/chat/', { messages, subject: subject || null });

// Streaming chat: calls onToken(text) for each delta, resolves with the final
// {tool_calls, sources, model} payload. Parses the backend's SSE stream.
export async function chatStream(
  messages: any[],
  subject: string | undefined,
  onToken: (t: string) => void
): Promise<any> {
  const res = await fetch(BASE + '/chat/stream', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, subject: subject || null }),
  });
  if (res.status === 401) throw new AuthError();
  if (!res.ok || !res.body)
    throw new Error(`${res.status} ${await res.text().catch(() => '')}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let final: any = {};
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const line = block.split('\n').find((l) => l.startsWith('data:'));
      if (!line) continue;
      let payload: any;
      try {
        payload = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }
      if (payload.token) onToken(payload.token);
      else if (payload.done) final = payload;
    }
  }
  return final;
}
