import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { NEEDS } from './ChatPanel.jsx';

const CATEGORY = { EMERGENCY: 'emergency', SAFETY: 'safety', PREPARE: 'prepare' };

/**
 * "My plan": the customer's actions (labeled EMERGENCY / SAFETY / PREPARE) and alert eligibility, from the same
 * rules the chat uses. Refetches when the declared needs or the storm shown on the map change.
 */
export default function PlanCard({ premise, needs, storm, regional, onError, refreshKey }) {
  const [open, setOpen] = useState(true);
  const [plan, setPlan] = useState(null);
  const [alerts, setAlerts] = useState(null);
  const ticket = useRef(0);

  useEffect(() => {
    if (!premise) return;
    const t = ++ticket.current;
    Promise.all([api.prepGuidance(premise, needs, storm), api.alertEligibility(premise, needs, storm)])
      .then(([p, a]) => { if (t === ticket.current) { setPlan(p); setAlerts(a); } })
      .catch((e) => onError?.(e.message));
  }, [premise, JSON.stringify(needs), JSON.stringify(storm), refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const rule = alerts?.rules_fired?.[0];
  const declared = NEEDS.filter((n) => needs.includes(n.id)).map((n) => n.label);
  return (
    <section className="panel plan-card" aria-label="My plan">
      <button type="button" className="plan-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span><span className="tag actions">your actions</span> My plan</span>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="plan-body">
          {!plan && <p className="hint-inline">Loading your plan…</p>}
          {plan && (
            <ul className="plan-actions">
              {plan.actions.slice(0, 5).map((a) => (
                <li key={a.text} className={CATEGORY[a.category]}>
                  <span className={`cat ${CATEGORY[a.category]}`}>{a.category}</span>
                  <span>{a.text}{a.why.startsWith('you told us') && <em> ({a.why})</em>}</span>
                </li>
              ))}
            </ul>
          )}
          {alerts && (
            <div className="plan-alerts">
              <b>Alerts</b>
              {alerts.escalate_to_human ? (
                <p>You qualify for a callback from an AEP representative under the alert rules.</p>
              ) : rule ? (
                <p>You qualify: {rule.action.toLowerCase()} by {rule.channel} ({rule.timing.toLowerCase()}).</p>
              ) : (
                <p>No automatic alert applies right now. You can still sign up for outage texts.</p>
              )}
            </div>
          )}
          <p className="plan-foot">
            {declared.length ? `Tailored to what you told us: ${declared.join(', ')}. ` : 'Tell us about needs at home in the chat to tailor this. '}
            {regional ? 'Based on a regional estimate for this location.' : 'Based on the model estimate for the day shown.'}
          </p>
        </div>
      )}
    </section>
  );
}
