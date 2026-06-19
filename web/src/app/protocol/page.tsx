"use client";
import { useMemo, useState } from "react";
import { parseText, AuthError } from "@/lib/api";

const SAMPLE = `Schedule of Activities
| Procedure | Screening | Cycle 1 Day 1 | Cycle 1 Day 8 | Week 4 | Follow-up |
|-----------|-----------|---------------|---------------|--------|-----------|
| Informed consent | X |  |  |  |  |
| Blood sample | X | X | X* | X | X |
| ECG | X |  |  |  |  |
| Drug administration |  | X |  |  |  |
| CT scan |  |  |  | X |  |

Footnotes:
* Blood sample on Cycle 1 Day 8 only if clinically indicated.

Screening visit includes informed consent, blood sample, and ECG.
Cycle 1 Day 1 visit includes blood sample and drug administration.
Week 4 visit includes blood sample, AE review, and CT scan.
Follow-up visit includes blood sample.`;

function abbrev(name: string, cycle?: string): string {
  if (cycle) return cycle;
  const n = name.toLowerCase();
  if (n.startsWith("screen")) return "SCR";
  if (n.startsWith("follow")) return "FU";
  const wk = n.match(/week\s*(\d+)/); if (wk) return "W" + wk[1];
  const cy = n.match(/cycle\s*(\d+)/); if (cy) return "C" + cy[1];
  return name.slice(0, 3).toUpperCase();
}

export default function ProtocolPage() {
  const [text, setText] = useState(SAMPLE);
  const [data, setData] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState(0);
  const [view, setView] = useState<"review" | "soa">("review");
  const [openConflict, setOpenConflict] = useState<string | null>(null);

  async function run() {
    setBusy(true); setMsg(""); setData(null);
    try { const r = await parseText(text); setData(r); setSel(0); setView("review"); }
    catch (e) { setMsg(e instanceof AuthError ? "Please sign in to parse." : String(e)); }
    finally { setBusy(false); }
  }

  const visits: any[] = data?.visits || [];
  const rows: any[] = data?.flat_rows || [];
  const conflicts: any[] = data?.conflicts || [];

  // conflicts grouped by visit name (count for the sidebar warning badge)
  const conflictsByVisit = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of conflicts) m[c.visit_name] = (m[c.visit_name] || 0) + 1;
    return m;
  }, [conflicts]);

  const rowsForVisit = (name: string) => rows.filter((r) => r.visit === name);

  // SoA matrix: unique activities (normalized) x visits
  const matrix = useMemo(() => {
    const acts: string[] = [];
    for (const r of rows) if (!acts.includes(r.normalized)) acts.push(r.normalized);
    const cell = (act: string, vname: string) =>
      rows.find((r) => r.normalized === act && r.visit === vname);
    const narrOnly = new Set(
      conflicts.filter((c) => c.code === "MissingInSoA").map((c) => c.activity_name),
    );
    return { acts, cell, narrOnly };
  }, [rows, conflicts]);

  function exportCsv() {
    const esc = (s: any) => `"${String(s ?? "").replace(/"/g, '""')}"`;
    const lines = ["visit,cycle,activity,normalized,source,system,condition"];
    for (const r of rows)
      lines.push([r.visit, r.cycle || "", r.activity, r.normalized, r.source, r.system, r.condition || ""].map(esc).join(","));
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    const a = document.createElement("a"); a.href = url; a.download = "ctms_rows.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  const srcBadge = (s: string) =>
    <span className={"badge " + (s === "narrative" ? "src-narr" : "src-soa")}>{s === "narrative" ? "narr" : "soa"}</span>;

  return (
    <div>
      <h1>Protocol review</h1>
      <div className="card">
        <label>Protocol text (Schedule of Activities + narrative)</label>
        <textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} />
        <div style={{ marginTop: 12 }}>
          <button className="btn" onClick={run} disabled={busy}>{busy ? "Parsing…" : "Parse protocol"}</button>
        </div>
        {msg && <p className="muted" style={{ marginTop: 8 }}>{msg}</p>}
      </div>

      {data && (
        <>
          <div className="card" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span className="badge primary">{visits.length} visits</span>
            <span className="badge ok">{rows.length} activities</span>
            <span className={"badge " + (conflicts.length ? "warn" : "ok")}>{conflicts.length} conflicts</span>
            <span style={{ flex: 1 }} />
            <div className="tabs" style={{ margin: 0 }}>
              <button className={"tab " + (view === "review" ? "active" : "")} onClick={() => setView("review")}>Review</button>
              <button className={"tab " + (view === "soa" ? "active" : "")} onClick={() => setView("soa")}>SoA matrix</button>
            </div>
            <button className="btn" onClick={exportCsv}>Export CSV</button>
          </div>

          {view === "review" ? (
            <div className="ws">
              {/* Sidebar */}
              <div className="ws-side">
                <div className="hdr">Visits ({visits.length})</div>
                {visits.map((v, i) => {
                  const warns = conflictsByVisit[v.name] || 0;
                  return (
                    <div key={i} className={"visit-item " + (i === sel ? "active" : "")} onClick={() => setSel(i)}>
                      <div className="nm">{v.name}</div>
                      <div className="mt">
                        <span className="badge gray">{abbrev(v.name, v.cycle_group)}</span>
                        <span className="badge gray">{rowsForVisit(v.name).length} acts</span>
                        {warns > 0 && <span className="badge warn">{warns}⚠</span>}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Main: selected visit activities */}
              <div className="card" style={{ margin: 0 }}>
                <h3 style={{ marginTop: 0 }}>{visits[sel]?.name}</h3>
                <table>
                  <thead><tr><th>Activity</th><th>Normalized</th><th>Source</th><th>System</th><th>Condition</th></tr></thead>
                  <tbody>
                    {rowsForVisit(visits[sel]?.name).map((r, i) => (
                      <tr key={i}>
                        <td><strong>{r.activity}</strong></td>
                        <td className="muted">{r.normalized}</td>
                        <td>{srcBadge(r.source)}</td>
                        <td>{r.system || "—"}</td>
                        <td>{r.condition || <span className="muted">—</span>}</td>
                      </tr>
                    ))}
                    {rowsForVisit(visits[sel]?.name).length === 0 && <tr><td colSpan={5} className="muted">No activities.</td></tr>}
                  </tbody>
                </table>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12, marginTop: 16 }}>
                  <div className="stat-card"><div className="val">{visits.length}</div><div className="lbl">Visits</div></div>
                  <div className="stat-card"><div className="val">{rows.length}</div><div className="lbl">Activities</div></div>
                  <div className="stat-card"><div className="val" style={{ color: "#d97706" }}>{conflicts.length}</div><div className="lbl">Conflicts</div></div>
                </div>
              </div>

              {/* Right: conflicts */}
              <div className="card" style={{ margin: 0 }}>
                <h3 style={{ marginTop: 0 }}>⚠️ Conflicts ({conflicts.length})</h3>
                {conflicts.length === 0 && <p><span className="badge ok">none</span></p>}
                {conflicts.map((c, i) => {
                  const id = "c" + i; const info = c.severity === "info";
                  return (
                    <div key={i} className={"conflict-card " + (info ? "info" : "")}>
                      <div className="ch" onClick={() => setOpenConflict(openConflict === id ? null : id)}>
                        <span>{info ? "ℹ️" : "⚠️"}</span>
                        <div>
                          <div className="ct">{c.visit_name}</div>
                          <div className="cd">{c.activity_name} — {c.code === "MissingInSoA" ? "Missing in SoA" : "Missing in Narrative"}</div>
                        </div>
                      </div>
                      {openConflict === id && <div className="cb">{c.message}</div>}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="card">
              <h3 style={{ marginTop: 0 }}>Schedule of Activities</h3>
              <div style={{ overflowX: "auto" }}>
                <table className="soa-grid">
                  <thead>
                    <tr>
                      <th className="proc">Procedure</th>
                      {visits.map((v, i) => <th key={i} style={{ textAlign: "center" }}>{v.name}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {matrix.acts.map((act, ri) => {
                      const narr = matrix.narrOnly.has(act);
                      return (
                        <tr key={ri} style={narr ? { background: "#fef3c7" } : undefined}>
                          <td className="proc">{act}{narr && <span className="badge warn" style={{ marginLeft: 6, fontSize: 9 }}>narr only</span>}</td>
                          {visits.map((v, ci) => {
                            const cell = matrix.cell(act, v.name);
                            return (
                              <td key={ci} style={{ textAlign: "center" }}>
                                {cell ? (cell.condition ? <span className="soa-c">X*</span> : <span className="soa-x">X</span>) : <span className="muted">—</span>}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div style={{ display: "flex", gap: 16, marginTop: 12, flexWrap: "wrap" }} className="muted">
                <span><span className="soa-x">X</span> Present</span>
                <span><span className="soa-c">X*</span> Conditional</span>
                <span>— Not collected</span>
                <span><span className="badge warn" style={{ fontSize: 9 }}>narr only</span> Narrative-only (conflict)</span>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
