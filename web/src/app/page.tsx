"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { me, security, auditVerify } from "@/lib/api";

export default function Dashboard() {
  const [user, setUser] = useState<any>(null);
  const [sec, setSec] = useState<any>(null);
  const [audit, setAudit] = useState<any>(null);

  useEffect(() => {
    me().then(setUser);
    security().then(setSec).catch(() => {});
    auditVerify().then(setAudit).catch(() => setAudit(null));
  }, []);

  return (
    <div>
      <h1>Clinical Research Platform</h1>
      <div className="card">
        <h3>Session</h3>
        {user ? (
          <p>Signed in as <strong>{user.username}</strong> (<span className="badge ok">{user.role}</span>)</p>
        ) : (
          <p className="muted">Not signed in. <Link href="/login">Sign in</Link> to access data.</p>
        )}
      </div>

      <div className="card">
        <h3>Security posture</h3>
        {sec?.security ? (
          <ul>
            <li>Auth required: <b>{String(sec.security.auth_required)}</b></li>
            <li>De-identification: <b>{String(sec.security.deidentification)}</b></li>
          </ul>
        ) : <p className="muted">—</p>}
        {audit && (
          <p>Audit chain: <span className={"badge " + (audit.ok ? "ok" : "warn")}>
            {audit.ok ? "verified" : "BROKEN"}</span> ({audit.count} entries)</p>
        )}
      </div>

      <div className="card">
        <h3>Modules</h3>
        <p>
          <Link href="/subjects">Subjects &amp; compliance</Link> · {" "}
          <Link href="/trips">Transport dispatch</Link> · {" "}
          <Link href="/protocol">Protocol parsing</Link> · {" "}
          <Link href="/chat">AI assistant</Link>
        </p>
      </div>
    </div>
  );
}
