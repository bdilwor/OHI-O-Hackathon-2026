// Current outage details: where, what area, why it takes X hours, reported and estimated fix (day month year).
const FMT = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
});
export const fmtDate = (iso) => (iso ? FMT.format(new Date(iso)).replace(/\b([ap])m\b/, (_, x) => `${x.toUpperCase()}M`) : '–');
const fmtHrs = (h) => (h < 1 ? `${Math.round(h * 60)} min` : `${h % 1 ? h.toFixed(1) : h} h`);

export default function OutageCard({ outage, test = false, onClose }) {
  if (!outage) return null;
  const restored = outage.status === 'restored';
  const h = outage.hours;
  const progress = Math.min(100, Math.round((100 * h.elapsed) / Math.max(h.p50, 0.1)));
  const a = outage.affected;
  return (
    <section className={`panel outage-card ${restored ? 'restored' : ''}`} aria-label="Outage details">
      <header>
        <span className={`dot ${restored ? 'ok' : 'out'}`} aria-hidden="true" />
        <div>
          <b>{restored ? 'Power restored' : outage.affects_you ? 'Outage affecting your home' : 'Outage nearby'}</b>
          <small>{test ? 'Outage test · simulated' : 'Reported by AEP'}</small>
        </div>
        {onClose && <button type="button" className="toast-x" aria-label="Hide outage details" onClick={onClose}>×</button>}
      </header>
      <dl>
        <dt>Cause</dt><dd>{outage.cause}</dd>
        <dt>Where</dt><dd>{outage.location}</dd>
        <dt>Area affected</dt>
        <dd>
          ~{a.homes.toLocaleString()} home{a.homes === 1 ? '' : 's'}
          {a.area_km2 > 0 && <> · {a.area_km2 < 0.1 ? `${Math.round(a.area_km2 * 1e6).toLocaleString()} m²` : `${a.area_km2.toFixed(2)} km²`}</>}
          {a.streets?.length > 0 && <> · around {a.streets.join(', ')}</>}
        </dd>
        <dt>Reported</dt><dd>{fmtDate(outage.reported_at)}</dd>
        <dt>{restored ? 'Restored' : 'Estimated fix'}</dt>
        <dd>
          <b>{fmtDate(outage.etr.p50)}</b>
          {!restored && <span className="range">likely between {fmtDate(outage.etr.p10)} and {fmtDate(outage.etr.p90)}</span>}
        </dd>
      </dl>
      {!restored && (
        <div className="eta">
          <div className="eta-track" aria-hidden="true"><div className="eta-fill" style={{ width: `${progress}%` }} /></div>
          <span>{fmtHrs(h.elapsed)} since reported · about {fmtHrs(h.remaining)} to go (model estimate)</span>
        </div>
      )}
      {!restored && (
        <details className="why" open>
          <summary>Why it will take about {fmtHrs(h.p50)}</summary>
          <ul>{outage.why.map((w) => <li key={w}>{w}</li>)}</ul>
        </details>
      )}
    </section>
  );
}
