import { useEffect, useRef, useState } from 'react';

export const OUTAGE_DEFAULTS = { device: 1, point: null, cause: 'tree', reported_hours_ago: 0, crews: 2, active: false };
const CAUSES = [
  ['tree', 'Tree on the power line'], ['pole', 'Broken utility pole'], ['vehicle', 'Vehicle hit a pole'],
  ['transformer', 'Transformer failure'], ['lightning', 'Lightning strike'], ['equipment', 'Equipment failure'],
];

/** Outage test controls. Simulated and read-only: nothing is saved or sent to AEP systems. */
export default function OutageTestPanel({ value, onChange, devices, onRestore }) {
  const [draft, setDraft] = useState(value);
  const timer = useRef(null);
  useEffect(() => { setDraft(value); }, [value]);
  useEffect(() => () => clearTimeout(timer.current), []);
  const update = (patch, debounce = 200) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => onChange(next), debounce);
  };
  const device = draft.point ? 'map' : String(draft.device);

  return (
    <section className="panel sandbox-panel" aria-label="Outage test controls">
      <h3><span className="tag sandbox">outage test</span> Simulated outage</h3>
      <label className="field">
        <span>Where is the fault?</span>
        <select className="input select" value={device}
                onChange={(e) => (e.target.value === 'map' ? null : update({ device: +e.target.value, point: null }, 0))}>
          {devices.map((d) => <option key={d.index} value={d.index}>{d.label}</option>)}
          {draft.point && <option value="map">Point clicked on the map</option>}
        </select>
        <small className="hint-inline">or click any pole, wire, transformer or fuse on the map</small>
      </label>
      <label className="field">
        <span>Cause</span>
        <select className="input select" value={draft.cause} onChange={(e) => update({ cause: e.target.value }, 0)}>
          {CAUSES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </label>
      <div className="field">
        <span>Reported: <span className="val">{draft.reported_hours_ago === 0 ? 'just now' : `${draft.reported_hours_ago} h ago`}</span></span>
        <input type="range" min="0" max="24" value={draft.reported_hours_ago} aria-label="Hours since the outage was reported"
               onChange={(e) => update({ reported_hours_ago: +e.target.value })} />
      </div>
      <div className="field">
        <span>Crews assigned: <span className="val">{draft.crews}</span></span>
        <input type="range" min="1" max="30" value={draft.crews} aria-label="Crews assigned"
               onChange={(e) => update({ crews: +e.target.value })} />
      </div>
      <div className="row wrap">
        {!draft.active
          ? <button type="button" className="btn small primary" onClick={() => update({ active: true }, 0)}>⚡ Start outage</button>
          : <button type="button" className="btn small primary" onClick={onRestore}>✓ Restore power</button>}
        <button type="button" className="btn small ghost" onClick={() => onChange({ ...OUTAGE_DEFAULTS })}>Reset</button>
      </div>
    </section>
  );
}
