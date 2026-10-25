import { useEffect, useRef, useState } from 'react';
import DeckGL from '@deck.gl/react';
import { ColumnLayer, ScatterplotLayer } from '@deck.gl/layers';
import Map from 'react-map-gl/maplibre';
import { api } from '../api.js';
import { BASEMAPS } from '../basemap.js';
import { outageColor, pct } from '../format.js';
import AuditDrawer from './AuditDrawer.jsx';
import ChatPanel from './ChatPanel.jsx';
import { Legend, MapControls, ViewToggle } from './MapPanels.jsx';
import OutageCard from './OutageCard.jsx';
import OutageTestPanel from './OutageTestPanel.jsx';
import RiskMap from './RiskMap.jsx';
import SandboxPanel, { SANDBOX_DEFAULTS } from './SandboxPanel.jsx';
import { stormLabel, useWorkspace } from './Workspace.jsx';

const AREA_VIEW = { '2d': { longitude: -79.95, latitude: 37.24, zoom: 9.6, pitch: 0, bearing: 0 },
  '3d': { longitude: -79.95, latitude: 37.18, zoom: 9.7, pitch: 50, bearing: -15 } };
const SAMPLE_PREMISE = '982542862';  // the demo customer, used only to preview message wording
const MESSAGES = [['pre_storm', 'Pre-storm'], ['outage_confirmed', 'Outage confirmed'],
  ['estimate_updated', 'Estimate updated'], ['restored', 'Restored']];
const FACILITY_COLOR = { hospital: [240, 82, 82], fire_station: [245, 165, 36], ambulance: [167, 139, 250] };
const BAND_CLASS = { Low: 'low', Medium: 'medium', High: 'high', 'Very High': 'very-high' };
const SIDE_TABS = [['storm', 'Storm'], ['outage', 'Outage test'], ['chat', 'Chat']];
const fmt = (n) => (n == null ? '–' : Number(n).toLocaleString());

function Kpi({ label, value, sub, tone }) {
  return (
    <div className={`kpi ${tone || ''}`}>
      <span className="kpi-label">{label}</span>
      <b className="kpi-value">{value}</b>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  );
}

/**
 * Internal AEP Operations page: one storm scenario drives the area-wide impact (KPIs, circuits, facilities) and,
 * for a chosen home, the detailed map, the outage test and the staff chat.
 */
export default function OpsDashboard({ home, space, setSpace, notify, onError, opsTab, onTestHome, mapTheme, onMapTheme, active }) {
  const w = useWorkspace({ kind: 'ops', home, space, setSpace, notify, onError });
  const { storm, mapData, messages, needs, demoSafe, bottleneck, outage, devices } = space;
  const [tab, setTab] = useState(opsTab || 'storm');
  const [scope, setScope] = useState('area');
  const [viewMode, setViewMode] = useState('2d');
  const [homeView, setHomeView] = useState('3d');
  const [layersOn, setLayersOn] = useState({ homes: true, grid: true, trees: true, path: true });
  const [areaCrews, setAreaCrews] = useState('');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [days, setDays] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [rules, setRules] = useState([]);
  const ticket = useRef(0);
  const last = useRef(null);
  const lastSandbox = useRef(storm.mode === 'sandbox' ? storm : SANDBOX_DEFAULTS);
  const wantHome = useRef(false);
  const homeReady = Boolean(home) && !w.needsAep;

  // The map follows the tool in use: Storm shows the area, Outage test and Chat show the chosen home.
  const pickTab = (id) => { setTab(id); setScope(id === 'storm' ? 'area' : 'home'); };
  useEffect(() => { if (opsTab) pickTab(opsTab); }, [opsTab]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (home && wantHome.current) { wantHome.current = false; setScope('home'); } }, [home]);
  useEffect(() => { if (w.params.active && homeReady) setScope('home'); }, [w.params.active]); // eslint-disable-line react-hooks/exhaustive-deps
  const showHome = scope === 'home' && homeReady;

  useEffect(() => {
    api.forecast().then((f) => setDays(f.days || [])).catch(() => {});
    api.alertEligibility(SAMPLE_PREMISE, [], null).then((a) => setRules(a.rules_table || [])).catch(() => {});
    Promise.all(MESSAGES.map(([kind]) => api.draftMessage(SAMPLE_PREMISE, kind).then((m) => [kind, m]).catch(() => [kind, null])))
      .then((pairs) => setDrafts(Object.fromEntries(pairs)));
  }, []);

  // Area-wide impact for the same scenario (the per-home crews slider doesn't apply area-wide).
  const sel = { ...storm, crews: areaCrews || undefined };
  useEffect(() => {
    const t = ++ticket.current;
    setLoading(true);
    const timer = setTimeout(() => {
      api.opsOverview(sel)
        .then((d) => {
          if (t !== ticket.current) return;
          const prev = last.current;
          if (prev && d.kpis.facilities_at_risk !== prev.kpis.facilities_at_risk) {
            const up = d.kpis.facilities_at_risk > prev.kpis.facilities_at_risk;
            notify([{ level: up ? 'warning' : 'info', title: 'Operations · Priority facilities',
              body: `${d.kpis.facilities_at_risk} critical facilities now on high-risk circuits (was ${prev.kpis.facilities_at_risk}).` }]);
          }
          last.current = d;
          setData(d);
        })
        .catch((e) => onError(e.message))
        .finally(() => { if (t === ticket.current) setLoading(false); });
    }, 200);
    return () => clearTimeout(timer);
  }, [JSON.stringify(sel.mode === 'sandbox' ? [sel.intensity, sel.duration_days, sel.crews] : [sel.day, sel.crews])]); // eslint-disable-line react-hooks/exhaustive-deps

  const setStorm = (st) => {
    if (st.mode === 'sandbox') lastSandbox.current = st;
    w.set({ storm: st });
  };

  const is3d = viewMode === '3d';
  // New layer instances each render (deck.gl diffs them by id); a remounted map can't reuse another map's layers.
  const layers = data ? [
    is3d ? new ColumnLayer({
      id: 'circuits-3d', data: data.map, pickable: true, diskResolution: 12, radius: 350, extruded: true,
      elevationScale: 1, getPosition: (d) => d.position, getElevation: (d) => 400 + d.expected_damage * 260,
      getFillColor: (d) => [...outageColor(d.main_line_p), 220],
      updateTriggers: { getFillColor: data.map.map((d) => d.main_line_p).join(), getElevation: data.map.map((d) => d.expected_damage).join() },
    }) : new ScatterplotLayer({
      id: 'circuits', data: data.map, pickable: true, radiusUnits: 'pixels', stroked: true, lineWidthMinPixels: 1,
      getPosition: (d) => d.position, getRadius: (d) => 4 + Math.sqrt(d.customers) / 4,
      getFillColor: (d) => [...outageColor(d.main_line_p), 200], getLineColor: [15, 23, 42, 200],
      updateTriggers: { getFillColor: data.map.map((d) => d.main_line_p).join() },
    }),
    new ScatterplotLayer({
      id: 'facilities', data: data.facilities, pickable: true, radiusUnits: 'pixels', stroked: true,
      lineWidthMinPixels: 2, getPosition: (d) => d.position, getRadius: 6,
      getFillColor: (d) => FACILITY_COLOR[d.type] || [255, 255, 255], getLineColor: [255, 255, 255],
    }),
  ] : [];

  const k = data?.kpis;
  const scenario = data?.storm;
  const sandbox = storm.mode === 'sandbox';
  const needHome = (what) => (
    <div className="ops-need-home">
      <p>{w.needsAep ? `${home.masked_address} is outside AEP's challenge data, and ${what} needs AEP's real equipment and grid map.`
        : `Choose a home to use ${what}.`}</p>
      <button type="button" className="btn primary small" onClick={() => { wantHome.current = true; onTestHome(); }}>
        {w.needsAep ? 'Choose an AEP-area location' : 'Choose a home'}
      </button>
    </div>
  );

  return (
    <main className="ops" aria-busy={loading}>
      {k && (
        <section className="kpis" aria-label="Key figures">
          <Kpi label="Expected damaged equipment" value={fmt(Math.round(k.expected_damaged_assets))} sub="poles, wires and devices" />
          <Kpi label="Customers likely out" value={fmt(k.expected_customers_out)} sub={`of ${fmt(k.total_customers)} in the area`}
               tone={k.expected_customers_out > 5000 ? 'bad' : k.expected_customers_out > 500 ? 'warn' : ''} />
          <Kpi label="High-risk circuits" value={`${k.high_risk_circuits} / ${k.circuits}`}
               sub={`${fmt(k.customers_high_risk_circuits)} customers on them`} tone={k.high_risk_circuits ? 'warn' : ''} />
          <Kpi label="Estimated crew-hours" value={fmt(k.crew_hours)}
               sub={k.hours_to_restore_all != null ? `~${fmt(k.hours_to_restore_all)} h with ${k.crews} crews` : 'incl. travel & setup (4.5x)'} />
          <Kpi label="Crews to restore in 24 h / 72 h" value={`${fmt(k.crews_for_24h)} / ${fmt(k.crews_for_72h)}`} sub="16-hour storm shifts" />
          <Kpi label="Critical facilities at high risk" value={k.facilities_at_risk} sub="hospitals, fire, ambulance"
               tone={k.facilities_at_risk ? 'bad' : ''} />
        </section>
      )}

      <div className="ops-stage">
        <section className="map-wrap ops-stage-map" aria-label={showHome ? 'Home map' : 'Service area risk map'}>
          {/* Both maps stay mounted at full size; the one behind keeps its tiles and meshes ready, so switching is instant. */}
          <div className={`ops-layer ${showHome ? 'behind' : ''}`} aria-hidden={showHome}>
              <DeckGL initialViewState={AREA_VIEW[viewMode]} controller layers={layers}
                      getTooltip={({ object: o }) => o && (o.circuit && o.customers != null
                        ? `Circuit ${o.circuit}\n${fmt(o.customers)} customers\nMain-line damage chance ${pct(o.main_line_p)}\nExpected damage ${o.expected_damage}`
                        : `${o.name}\n${o.type_label} · ${o.risk_band} risk (circuit ${o.circuit})`)}>
                <Map mapStyle={BASEMAPS[mapTheme]} reuseMaps attributionControl />
              </DeckGL>
              <div className="panel ops-map-bar">
                <ViewToggle viewMode={viewMode} onViewMode={setViewMode} theme={mapTheme} onTheme={onMapTheme} />
                <p className="legend-note">{is3d ? 'Column height = expected damage' : 'Dot size = customers'} · color = main-line
                  damage chance · bright dots = critical facilities</p>
              </div>
          </div>
          {homeReady && (
            <div className={`ops-layer ${showHome ? '' : 'behind'}`} aria-hidden={!showHome}>
              <RiskMap data={mapData} viewMode={homeView} layersOn={layersOn} bottleneck={outage?.bottleneck || bottleneck}
                       onMapClick={([lon, lat]) => w.setParams({ ...w.params, point: [lon, lat] })} wind={w.wind}
                       pathState={w.pathState} footprint={sandbox && mapData ? storm.radius_m : null} outage={outage}
                       basemap={BASEMAPS[mapTheme]} active={active && showHome} />
              <MapControls viewMode={homeView} onViewMode={setHomeView} theme={mapTheme} onTheme={onMapTheme} storm={storm}
                           showForecast={false} layersOn={layersOn} loading={w.loading}
                           onToggle={(id) => setLayersOn((l) => ({ ...l, [id]: !l[id] }))} />
              <OutageCard outage={outage} test />
              {!outage && <Legend stormLabel={stormLabel(mapData?.storm)} />}
            </div>
          )}
          <div className="segmented two ops-scope" role="group" aria-label="Map scope">
            <button type="button" aria-pressed={!showHome} onClick={() => setScope('area')}>Service area</button>
            <button type="button" aria-pressed={showHome} disabled={!homeReady} onClick={() => setScope('home')}
                    title={homeReady ? undefined : 'Choose a home on the Outage test or Chat tab'}>
              {homeReady ? home.masked_address : 'Home'}
            </button>
          </div>
        </section>

        <aside className="side ops-side" aria-label="Scenario, outage test and chat">
          <div className="ops-tabs" role="tablist" aria-label="Operations tools">
            {SIDE_TABS.map(([id, label]) => (
              <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => pickTab(id)}>{label}</button>
            ))}
          </div>
          <div className={`ops-tab-body ${tab === 'chat' ? 'chat-tab' : ''}`} role="tabpanel">
            {tab === 'storm' && (
              <>
                <div className="segmented two" role="group" aria-label="Scenario source">
                  <button type="button" aria-pressed={!sandbox} onClick={() => setStorm({ mode: 'forecast', day: storm.day ?? 0 })}>Live forecast</button>
                  <button type="button" aria-pressed={sandbox} onClick={() => setStorm(lastSandbox.current)}>Simulated storm</button>
                </div>
                {sandbox ? (
                  <SandboxPanel value={storm} onChange={setStorm} restoration={homeReady ? mapData?.restoration : null} />
                ) : (
                  <label className="field">
                    <span>Forecast day: <span className="val">{days[storm.day ?? 0] ? `${days[storm.day ?? 0].weekday} · ${days[storm.day ?? 0].label} (gusts ${days[storm.day ?? 0].gust_mph} mph)` : storm.day ?? 0}</span></span>
                    <input type="range" min="0" max="6" value={storm.day ?? 0} aria-label="Forecast day"
                           onChange={(e) => setStorm({ mode: 'forecast', day: +e.target.value })} />
                  </label>
                )}
                <label className="field crews-field">
                  <span>Area crews available (optional)</span>
                  <input className="input" type="number" min="1" max="200" placeholder="e.g. 120" value={areaCrews}
                         onChange={(e) => setAreaCrews(e.target.value ? Math.max(1, Math.min(200, +e.target.value)) : '')} />
                </label>
                <p className="ops-storm">{loading ? 'Updating…' : scenario ? <>Scenario: <b>{scenario.summary}</b>{scenario.when && !sandbox ? ` (${scenario.when})` : ''}. <span className="tag demo">model estimate</span></> : null}</p>
              </>
            )}
            {tab === 'outage' && (homeReady
              ? <OutageTestPanel value={w.params} onChange={w.setParams} devices={devices || []} onRestore={w.restore} />
              : needHome('the outage test'))}
            {tab === 'chat' && (homeReady || !home ? (
              <>
                <ChatPanel messages={messages} busy={w.busy} onSend={w.send} disabled={!homeReady} area="ops"
                           needs={needs} onToggleNeed={w.toggleNeed} demoSafe={demoSafe} onDemoSafe={(v) => w.set({ demoSafe: v })} />
                {!homeReady && needHome('the staff chat')}
                <AuditDrawer refreshKey={messages.length} />
              </>
            ) : needHome('the staff chat'))}
          </div>
        </aside>
      </div>

      <div className="ops-grid">
        <section className="ops-card" aria-label="Riskiest circuits">
          <h3>Riskiest circuits</h3>
          <table className="ops-table">
            <thead><tr><th>Circuit</th><th>Customers</th><th>Exp. damage</th><th>Main line</th><th>Most damage from</th><th>Facilities</th></tr></thead>
            <tbody>
              {data?.circuits.map((c) => (
                <tr key={c.circuit}>
                  <td>{c.circuit}</td><td>{fmt(c.customers)}</td><td>{c.expected_damage}</td>
                  <td><span className={`band ${BAND_CLASS[c.risk_band]}`}>{pct(c.main_line_p)}</span></td>
                  <td>{c.top_equipment || '–'}</td><td title={c.facilities.join(', ')}>{c.facilities.length || '–'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="ops-card" aria-label="Priority facilities">
          <h3>Priority facilities <span className="hint-inline">public data · internal view only</span></h3>
          <ul className="facility-list">
            {data?.facilities.slice(0, 12).map((f) => (
              <li key={`${f.name}-${f.position}`}>
                <span className={`band ${BAND_CLASS[f.risk_band]}`}>{f.risk_band}</span>
                <div><b>{f.name}</b><span className="meta">{f.type_label} · circuit {f.circuit}</span>
                  <span className="action">{f.action}</span></div>
              </li>
            ))}
          </ul>
        </section>

        <section className="ops-card" aria-label="Proactive communications">
          <h3>Proactive customer communications</h3>
          <p className="hint-inline">Previews for the demo customer; every message is ≤ 320 characters and plain language.</p>
          <div className="drafts">
            {MESSAGES.map(([kind, label]) => (
              <div key={kind} className="draft">
                <span className="tag customer">{label}</span>
                <p>{drafts[kind]?.text || '…'}</p>
              </div>
            ))}
          </div>
          {rules.length > 0 && (
            <details className="rules">
              <summary>Alert and escalation rules ({rules.length})</summary>
              <table className="ops-table">
                <thead><tr><th>When</th><th>Action</th><th>Channel</th><th>Timing</th></tr></thead>
                <tbody>{rules.map((r) => <tr key={r.id}><td>{r.when}</td><td>{r.action}</td><td>{r.channel}</td><td>{r.timing}</td></tr>)}</tbody>
              </table>
            </details>
          )}
        </section>

        <section className="ops-card" aria-label="Assumptions and audit">
          <h3>How these numbers are made</h3>
          <ul className="assumptions">{data?.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
          <AuditDrawer refreshKey={JSON.stringify(sel)} />
        </section>
      </div>
    </main>
  );
}
