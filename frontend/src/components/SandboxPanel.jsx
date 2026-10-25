import { useEffect, useRef, useState } from 'react';

export const SANDBOX_DEFAULTS = { mode: 'sandbox', intensity: 3, crews: 4, duration_days: 1, radius_m: 1500 };
const NAMES = { 1: 'Breezy', 2: 'Windy', 3: 'Strong storm', 4: 'Severe storm', 5: 'Extreme storm' };
const YD_PER_M = 1.09361;

/** Demo/testing controls. Everything here is simulated client-side + read-only model calls; nothing is saved. */
export default function SandboxPanel({ value, onChange, onSimulateOutage, restoration }) {
  const [draft, setDraft] = useState(value);
  const [unit, setUnit] = useState('m');
  const [playing, setPlaying] = useState(false);
  const timer = useRef(null);
  const play = useRef(null);

  // Debounce slider drags so the map recomputes once the hand stops (~200 ms).
  useEffect(() => { setDraft(value); }, [value]);
  const update = (patch) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => onChange(next), 200);
  };
  useEffect(() => () => { clearTimeout(timer.current); clearInterval(play.current); }, []);

  function playStorm() {
    const peak = draft.intensity;
    const steps = [...Array(peak).keys()].map((i) => i + 1);
    const seq = [...steps, ...steps.slice(0, -1).reverse()];
    let i = 0;
    setPlaying(true);
    clearInterval(play.current);
    onChange({ ...draft, intensity: seq[0] });
    play.current = setInterval(() => {
      i += 1;
      if (i >= seq.length) { clearInterval(play.current); setPlaying(false); onChange({ ...draft, intensity: peak }); return; }
      onChange({ ...draft, intensity: seq[i] });
    }, 2200);
  }

  const radiusShown = unit === 'm' ? draft.radius_m : Math.round(draft.radius_m * YD_PER_M);
  return (
    <section className="panel sandbox-panel" aria-label="Sandbox storm controls">
      <h3><span className="tag sandbox">sandbox</span> Simulated storm</h3>
      <div className="field">
        <span>Storm intensity: <span className="val">{draft.intensity} · {NAMES[draft.intensity]}</span></span>
        <input type="range" min="1" max="5" value={draft.intensity} onChange={(e) => update({ intensity: +e.target.value })}
               aria-label="Storm intensity, 1 to 5" />
        <div className="ticks" aria-hidden="true"><span>1</span><span>2</span><span>3</span><span>4</span><span>5</span></div>
      </div>
      <div className="field">
        <span>Available crews: <span className="val">{draft.crews}</span></span>
        <input type="range" min="1" max="30" value={draft.crews} onChange={(e) => update({ crews: +e.target.value })}
               aria-label="Available crews" />
      </div>
      <div className="field">
        <span>Storm duration: <span className="val">{draft.duration_days} day{draft.duration_days > 1 ? 's' : ''}</span></span>
        <input type="range" min="1" max="5" value={draft.duration_days}
               onChange={(e) => update({ duration_days: +e.target.value })} aria-label="Storm duration in days" />
      </div>
      <div className="field">
        <span className="row">
          Affected area: <span className="val">{radiusShown.toLocaleString()} {unit === 'm' ? 'm' : 'yd'}</span>
          <span className="unit-toggle" role="group" aria-label="Distance unit">
            {['m', 'yd'].map((u) => (
              <button key={u} type="button" aria-pressed={unit === u} onClick={() => setUnit(u)}>{u}</button>
            ))}
          </span>
        </span>
        <input type="range" min="100" max="5000" step="100" value={draft.radius_m}
               onChange={(e) => update({ radius_m: +e.target.value })} aria-label="Affected area radius" />
      </div>
      {restoration?.p50_hrs != null && (
        <p className="sandbox-readout">
          If power goes out: most likely <b>~{restoration.p50_hrs} h</b> (likely {restoration.p10_hrs}–{restoration.p90_hrs} h)
        </p>
      )}
      <div className="row wrap">
        <button type="button" className="btn small primary" onClick={playStorm} disabled={playing}>
          {playing ? 'Playing…' : '▶ Play storm'}
        </button>
        <button type="button" className="btn small" onClick={onSimulateOutage}>Simulate outage at my home</button>
        <button type="button" className="btn small ghost" onClick={() => { clearInterval(play.current); setPlaying(false); onChange(SANDBOX_DEFAULTS); }}>
          Reset
        </button>
      </div>
    </section>
  );
}
