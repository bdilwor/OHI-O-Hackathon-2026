import { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function AuditDrawer({ refreshKey }) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState([]);

  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    const load = () => api.audit(25).then((d) => alive && setEntries(d.entries)).catch(() => {});
    load();
    const id = setInterval(load, 4000);
    return () => { alive = false; clearInterval(id); };
  }, [open, refreshKey]);

  return (
    <details className="audit" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Privacy &amp; audit log: every tool call, what was returned, what was redacted</summary>
      <div className="audit-list">
        {entries.length === 0 && <span className="conf">No calls yet.</span>}
        {entries.map((e, i) => (
          <div className="audit-entry" key={`${e.ts}-${i}`}>
            <div><code>{e.tool}</code> · {e.source}{e.provider ? ` · ${e.provider}` : ''} · {new Date(e.ts).toLocaleTimeString()}</div>
            {e.input?.premise && <div className="conf">home {e.input.premise}</div>}
            {e.fields_redacted?.length > 0 && (
              <div>{e.fields_redacted.map((f) => <span key={f} className="redacted">removed: {f}</span>)}</div>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}
