import { LAYERS } from './RiskMap.jsx';

const LABEL_DOT = { Calm: '#2ecc8f', Breezy: '#c6d84a', Windy: '#f5a524', Storm: '#f2711c', 'Severe storm': '#f05252' };

export function MapControls({ viewMode, onViewMode, storm, onStorm, forecast, layersOn, onToggle, loading,
                             showForecast = true }) {
  const days = showForecast ? forecast?.days || [] : [];
  const day = days[storm?.day ?? 0];
  return (
    <section className="panel layers-panel" aria-label="Map controls">
      <h3>View {loading && <span className="conf">updating…</span>}</h3>
      <div className="segmented two" role="group" aria-label="Map view">
        {['2d', '3d'].map((m) => (
          <button key={m} type="button" aria-pressed={viewMode === m} onClick={() => onViewMode(m)}>{m.toUpperCase()}</button>
        ))}
      </div>

      {showForecast && <h3 style={{ marginTop: 12 }}>Weather forecast <span className="tag model">live</span></h3>}
      {showForecast && forecast && !forecast.available && <p className="legend-note">Live forecast unavailable; assuming calm weather.</p>}
      {days.length > 0 && (
        <div>
          <label htmlFor="day" className="sr-only">Forecast day</label>
          <input id="day" type="range" min="0" max={days.length - 1} value={storm.day ?? 0}
                 onChange={(e) => onStorm({ mode: 'forecast', day: +e.target.value })} className="day-slider" />
          <div className="day-ticks" aria-hidden="true">
            {days.map((d) => (
              <span key={d.date} title={`${d.label}: gusts ${d.gust_mph} mph`}>
                <i style={{ background: LABEL_DOT[d.label] }} />{d.weekday}
              </span>
            ))}
          </div>
          {day && (
            <p className="day-summary">
              <b>{day.weekday === 'Today' ? 'Today' : `${day.weekday} ${day.date.slice(5)}`}:</b> {day.label} ·{' '}
              {day.conditions.toLowerCase()} · gusts {Math.round(day.gust_mph)} mph
            </p>
          )}
        </div>
      )}

      <h3 style={{ marginTop: 12 }}>Layers</h3>
      {LAYERS.map((l) => (
        <label key={l.id} className="toggle-row">
          <input type="checkbox" checked={layersOn[l.id]} onChange={() => onToggle(l.id)}
                 disabled={l.id === 'trees' && viewMode !== '3d'} />
          <span className="swatch" style={{ background: l.swatch }} aria-hidden="true" />
          {l.label}
        </label>
      ))}
    </section>
  );
}

export function Legend({ stormLabel }) {
  return (
    <section className="panel legend-panel" aria-label="Map legend">
      <h3>Outage severity <span className="tag model">model estimate</span></h3>
      <div className="scale-bar" aria-hidden="true" />
      <div className="scale-labels"><span>Good</span><span>Bad</span></div>
      <p className="legend-note">
        <b>Homes &amp; trees</b>: chance of an outage (green under 5%, red 50%+).{' '}
        <b>Poles, equipment &amp; wires</b>: chance that item is damaged (green under 1%, red 20%+).
        {stormLabel && <> Showing: <b>{stormLabel}</b>.</>}
      </p>
    </section>
  );
}
