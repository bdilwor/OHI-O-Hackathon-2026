import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { diffSnapshots } from '../notify.js';
import RiskMap from './RiskMap.jsx';
import { Legend, MapControls } from './MapPanels.jsx';
import ChatPanel from './ChatPanel.jsx';
import AuditDrawer from './AuditDrawer.jsx';
import SandboxPanel from './SandboxPanel.jsx';
import OutageCard from './OutageCard.jsx';
import OutageTestPanel, { OUTAGE_DEFAULTS } from './OutageTestPanel.jsx';

const POLL_MS = 5 * 60 * 1000; // live forecast refresh (backend caches it for an hour)
let nextId = 1;
export const msg = (m) => ({ id: `m${nextId++}`, ...m });

/** Notifications for an outage record changing (appears, estimate moves, restored). */
function diffOutage(prev, next, test) {
  const tag = test ? 'Outage test · ' : '';
  if (!prev && next && next.status !== 'restored') {
    return [{ level: 'danger', title: `${tag}${next.affects_you ? 'Outage at your home' : 'Outage nearby'}`,
              body: `${next.cause} near you. About ${next.affected.homes} homes affected. Estimated fix: ${next.etr_display.p50}.` }];
  }
  if (prev && next && prev.status !== 'restored' && next.status === 'restored') {
    return [{ level: 'success', title: `${tag}Power restored`, body: 'Power has been restored. Thanks for your patience.' }];
  }
  if (prev && next && next.status !== 'restored' && Math.abs(next.hours.p50 - prev.hours.p50) >= 1) {
    return [{ level: next.hours.p50 > prev.hours.p50 ? 'warning' : 'info', title: `${tag}Estimated fix time updated`,
              body: `Now ${next.etr_display.p50} (likely ${next.etr_display.p10} to ${next.etr_display.p90}).` }];
  }
  if (prev && next && (prev.location !== next.location || prev.cause !== next.cause)) {
    return [{ level: 'info', title: `${tag}Outage details updated`, body: `${next.cause}: ${next.location}.` }];
  }
  return [];
}

function stormLabel(storm) {
  if (!storm) return null;
  if (storm.sandbox) return `simulated category ${storm.intensity} storm (${storm.label.toLowerCase()})`;
  return storm.when ? `${storm.label.toLowerCase()} weather ${storm.when}` : storm.label;
}

function windLevel(mapData, sandbox) {
  if (!mapData?.storm) return 0;
  if (sandbox) return (mapData.storm.intensity - 1) / 4;
  const day = mapData.forecast?.days?.[mapData.storm.lead_days ?? 0];
  return Math.max(0, Math.min(1, ((day?.gust_mph ?? 0) - 15) / 45));
}

/**
 * One map + chat area. `space` holds this area's persistent state (storm setting, map snapshot, chat), lifted
 * to App so switching between My home and Sandbox keeps both.
 */
export default function Workspace({ kind, home, space, setSpace, notify, onMapClick, onError }) {
  const sandbox = kind === 'sandbox';
  const outageTab = kind === 'outage';
  const [viewMode, setViewMode] = useState('3d');
  const [layersOn, setLayersOn] = useState({ homes: true, grid: true, trees: true, path: true });
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const { storm, mapData, messages, needs, demoSafe, bottleneck, session, outage, devices } = space;
  const params = space.outageParams || OUTAGE_DEFAULTS;
  const premise = home?.premise;
  const lastSnap = useRef(mapData);
  const set = (patch) => setSpace((s) => ({ ...s, ...(typeof patch === 'function' ? patch(s) : patch) }));

  // Fetch the model's prediction; diff against the previous snapshot for notifications.
  const refresh = useRef(null);
  const latest = useRef(0);
  refresh.current = async (quiet = false) => {
    if (!premise) return;
    const ticket = ++latest.current;
    if (!quiet) setLoading(true);
    try {
      const d = await api.mapContext(premise, storm);
      if (ticket !== latest.current) return; // a newer slider position is already on its way
      notify(diffSnapshots(lastSnap.current, d, { sandbox }));
      lastSnap.current = d;
      set({ mapData: d });
    } catch (e) {
      onError(e.message);
    } finally {
      if (ticket === latest.current) setLoading(false);
    }
  };
  useEffect(() => { refresh.current(); }, [premise, JSON.stringify(storm)]); // eslint-disable-line react-hooks/exhaustive-deps

  // Outages: the feed on My home, the simulated fault on Outage test. Only the latest response is applied.
  const lastOutage = useRef(outage);
  const outageTicket = useRef(0);
  const applyOutage = (next) => {
    notify(diffOutage(lastOutage.current, next, outageTab));
    if (outageTab && next && !lastOutage.current && next.status !== 'restored') {
      set((s) => ({ messages: [...s.messages, msg({ role: 'assistant', proactive: true,
        text: `Outage test: ${next.cause.toLowerCase()} at ${next.location.charAt(0).toLowerCase()}${next.location.slice(1)}. ` +
              `${next.affects_you ? 'Your home is affected. ' : 'Your home is not affected. '}` +
              `Estimated fix: ${next.etr_display.p50} (likely between ${next.etr_display.p10} and ${next.etr_display.p90}).` })] }));
    }
    lastOutage.current = next;
    set({ outage: next });
  };
  const loadOutage = useRef(null);
  loadOutage.current = async () => {
    if (!premise || sandbox) return;
    const ticket = ++outageTicket.current;
    try {
      let next = null;
      if (outageTab) next = params.active ? await api.outageTest(premise, params) : (outage?.status === 'restored' ? outage : null);
      else next = (await api.outageCurrent(premise)).outage;
      if (ticket === outageTicket.current) applyOutage(next);
    } catch (e) {
      if (ticket === outageTicket.current) onError(e.message);
    }
  };
  useEffect(() => { loadOutage.current(); }, [premise, JSON.stringify(params)]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!outageTab || !premise || devices) return;
    api.outageDevices(premise).then((d) => set({ devices: d.devices })).catch((e) => onError(e.message));
  }, [outageTab, premise]); // eslint-disable-line react-hooks/exhaustive-deps
  const setParams = (p) => set({ outageParams: p });
  const restore = () => {
    if (!outage) return;
    const restored = { ...outage, status: 'restored', etr: { ...outage.etr, p50: new Date().toISOString() } };
    applyOutage(restored);
    set((s) => ({ outageParams: { ...params, active: false },
      messages: [...s.messages, msg({ role: 'assistant', proactive: true, text: 'Outage test: power has been restored.' })] }));
  };

  // My home: poll the live forecast, and listen for real outage events for this home.
  useEffect(() => {
    if (sandbox || !premise) return undefined;
    const id = setInterval(() => { refresh.current(true); loadOutage.current(); }, POLL_MS);
    const es = new EventSource(api.stormEventsUrl);
    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'hello' || !lastSnap.current?.demo_home) return;
      loadOutage.current(); // the outage panel follows the feed
      const level = data.kind === 'restored' ? 'success' : data.kind === 'pre_storm' ? 'warning' : 'danger';
      notify([{ level, title: { outage_confirmed: 'Outage confirmed', restored: 'Power restored',
        estimate_updated: 'Restoration estimate updated', pre_storm: 'Storm alert' }[data.kind] || 'Update', body: data.text }]);
      set((s) => ({ messages: [...s.messages, msg({ role: 'assistant', text: data.text, proactive: true })] }));
    };
    return () => { clearInterval(id); es.close(); };
  }, [sandbox, premise]); // eslint-disable-line react-hooks/exhaustive-deps

  async function send(text) {
    set((s) => ({ messages: [...s.messages, msg({ role: 'user', text })] }));
    setBusy(true);
    try {
      const activeOutage = outageTab && params.active
        ? { cause: params.cause, reported_hours_ago: params.reported_hours_ago, crews: params.crews,
            ...(params.point ? { lat: params.point[1], lon: params.point[0] } : { device: params.device }) }
        : undefined;
      const r = await api.chat({ message: text, session_id: session, premise, declared_needs: needs,
                                 demo_safe: demoSafe, storm, outage: activeOutage });
      set((s) => ({
        messages: [...s.messages, msg({ role: 'assistant', text: r.reply, qtype: r.question_type,
          changers: r.what_could_change, confidence: r.confidence, storm: r.storm, mode: r.mode, provider: r.provider,
          tools: r.tools_used, fallback: r.fallback })],
        bottleneck: r.bottleneck || s.bottleneck,
      }));
    } catch (e) {
      set((s) => ({ messages: [...s.messages, msg({ role: 'assistant', text: `Sorry, something went wrong: ${e.message}. ` +
        'If you see a downed line, stay 35 feet away and call 911.' })] }));
    } finally {
      setBusy(false);
    }
  }

  const pathState = outage?.status === 'restored' ? 'restored' : outage?.affects_you ? 'outage' : 'normal';
  const mapClick = !home ? onMapClick
    : outageTab ? ([lon, lat]) => setParams({ ...params, point: [lon, lat] }) : null;

  return (
    <main className="main">
      <div className="map-wrap">
        <RiskMap data={mapData} viewMode={viewMode} layersOn={layersOn} bottleneck={outage?.bottleneck || bottleneck}
                 onMapClick={mapClick} wind={windLevel(mapData, sandbox)} pathState={pathState}
                 footprint={sandbox && mapData ? storm.radius_m : null} outage={outage} />
        {home && (
          <>
            <MapControls viewMode={viewMode} onViewMode={setViewMode} storm={storm} showForecast={kind === 'home'}
                         onStorm={(st) => set({ storm: st })} forecast={mapData?.forecast} layersOn={layersOn}
                         loading={loading} onToggle={(id) => setLayersOn((l) => ({ ...l, [id]: !l[id] }))} />
            {sandbox && (
              <SandboxPanel value={storm} onChange={(st) => set({ storm: st })} restoration={mapData?.restoration} />
            )}
            {outageTab && (
              <OutageTestPanel value={params} onChange={setParams} devices={devices || []} onRestore={restore} />
            )}
            {kind === 'home' && !outage && <div className="no-outage">✓ No outages reported at your home</div>}
            {!sandbox && <OutageCard outage={outage} test={outageTab} />}
            <Legend stormLabel={stormLabel(mapData?.storm)} />
          </>
        )}
      </div>
      <aside className="side" aria-label="Chat with the outage concierge">
        <ChatPanel messages={messages} busy={busy} onSend={send} disabled={!home} area={kind}
                   needs={needs} onToggleNeed={(id) => set((s) => ({ needs: s.needs.includes(id) ? s.needs.filter((x) => x !== id) : [...s.needs, id] }))}
                   demoSafe={demoSafe} onDemoSafe={(v) => set({ demoSafe: v })} />
        <AuditDrawer refreshKey={messages.length} />
      </aside>
    </main>
  );
}
