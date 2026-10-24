// What changed between two map snapshots for the same home -> notifications.
// Snapshots are /map/context payloads (forecast, storm shown, your risk, restoration, homes).

const BAND_ORDER = ['Low', 'Medium', 'High', 'Very High'];
const OUTAGE_P = 0.5;
const PROB_STEP = 0.1;
const REPAIR_STEP_H = 2;
const HOMES_OUT_THRESHOLDS = [10, 50];

const pctTxt = (p) => (p < 0.01 ? 'under 1%' : `${Math.round(p * 100)}%`);
const homesOut = (snap) => (snap?.homes || []).filter((h) => !h.is_you && h.outage_p >= OUTAGE_P).length;
const dayName = (d) => (d.weekday === 'Today' ? 'Today' : `${d.weekday} ${d.date.slice(5)}`);

export function diffSnapshots(prev, next, { sandbox = false } = {}) {
  if (!prev || !next || prev.home?.join() !== next.home?.join()) return [];
  const out = [];
  const tag = sandbox ? 'Sandbox · ' : '';

  // Weather: any forecast day whose outlook changed (live forecast only).
  if (!sandbox) {
    const before = Object.fromEntries((prev.forecast?.days || []).map((d) => [d.date, d]));
    for (const d of next.forecast?.days || []) {
      const p = before[d.date];
      if (p && p.label !== d.label) {
        const worse = d.storm_n > p.storm_n;
        out.push({ level: worse ? 'warning' : 'info', title: `Weather update: ${dayName(d)}`,
                   body: `Now forecast ${d.label.toLowerCase()} (was ${p.label.toLowerCase()}), gusts up to ${Math.round(d.gust_mph)} mph.` });
      }
    }
  }

  // Your outage chance.
  const a = prev.your_risk || {};
  const b = next.your_risk || {};
  if (b.probability != null && a.probability != null) {
    if (b.probability >= OUTAGE_P && a.probability < OUTAGE_P) {
      out.push({ level: 'danger', title: `${tag}Outage likely at your home`,
                 body: `The model now puts your chance of losing power at ${pctTxt(b.probability)} (${b.risk_band}).` });
    } else if (a.risk_band !== b.risk_band) {
      const worse = BAND_ORDER.indexOf(b.risk_band) > BAND_ORDER.indexOf(a.risk_band);
      out.push({ level: worse ? 'warning' : 'success', title: `${tag}Outage risk ${worse ? 'up' : 'down'}: ${b.risk_band}`,
                 body: `Your chance of losing power is now ${pctTxt(b.probability)} (was ${pctTxt(a.probability)}).` });
    } else if (Math.abs(b.probability - a.probability) >= PROB_STEP) {
      out.push({ level: 'info', title: `${tag}Outage chance changed`,
                 body: `Now ${pctTxt(b.probability)} (was ${pctTxt(a.probability)}).` });
    }
  }

  // Repair time if the power goes out.
  const ra = prev.restoration || {};
  const rb = next.restoration || {};
  if (ra.p50_hrs != null && rb.p50_hrs != null && Math.abs(rb.p50_hrs - ra.p50_hrs) >= REPAIR_STEP_H) {
    const later = rb.p50_hrs > ra.p50_hrs;
    out.push({ level: later ? 'warning' : 'success', title: `${tag}Repair estimate ${later ? 'longer' : 'shorter'}`,
               body: `If the power goes out: most likely ~${rb.p50_hrs} h (likely ${rb.p10_hrs}–${rb.p90_hrs} h), was ~${ra.p50_hrs} h.` });
  }

  // Many neighbors predicted out.
  const [na, nb] = [homesOut(prev), homesOut(next)];
  for (const t of HOMES_OUT_THRESHOLDS) {
    if (na < t && nb >= t) {
      out.push({ level: 'danger', title: `${tag}Widespread outages likely nearby`,
                 body: `${nb} homes within 1 km are now predicted to lose power.` });
      break;
    }
  }
  return out;
}
