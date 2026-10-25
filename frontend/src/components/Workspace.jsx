import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { diffSnapshots } from '../notify.js';
import RiskMap from './RiskMap.jsx';
import { Legend, MapControls } from './MapPanels.jsx';
import ChatPanel from './ChatPanel.jsx';
import AuditDrawer from './AuditDrawer.jsx';
import SandboxPanel from './SandboxPanel.jsx';

const POLL_MS = 5 * 60 * 1000; // live forecast refresh (backend caches it for an hour)
let nextId = 1;
export const msg = (m) => ({ id: `m${nextId++}`, ...m });

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
  const [viewMode, setViewMode] = useState('3d');
  const [layersOn, setLayersOn] = useState({ homes: true, grid: true, trees: true, path: true });
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const { storm, mapData, messages, needs, demoSafe, bottleneck, session } = space;
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

  // My home: poll the live forecast, and listen for real outage events for this home.
  useEffect(() => {
    if (sandbox || !premise) return undefined;
    const id = setInterval(() => refresh.current(true), POLL_MS);
    const es = new EventSource(api.stormEventsUrl);
    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'hello' || !lastSnap.current?.demo_home) return;
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
      const r = await api.chat({ message: text, session_id: session, premise, declared_needs: needs,
                                 demo_safe: demoSafe, storm });
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

  function simulateOutage() {
    const r = mapData?.restoration;
    const range = r?.p50_hrs != null
      ? ` Estimated restoration (model): most likely ~${r.p50_hrs} h, likely ${r.p10_hrs}–${r.p90_hrs} h.` : '';
    const text = `SIMULATED: an outage is affecting your home.${range} Stay 35 feet from any downed line and call 911 if you see one.`;
    notify([{ level: 'danger', title: 'Sandbox · Outage at your home', body: text.replace('SIMULATED: ', '') }]);
    set((s) => ({ messages: [...s.messages, msg({ role: 'assistant', text, proactive: true })] }));
  }

  return (
    <main className="main">
      <div className="map-wrap">
        <RiskMap data={mapData} viewMode={viewMode} layersOn={layersOn} bottleneck={bottleneck}
                 onMapClick={home ? null : onMapClick} wind={windLevel(mapData, sandbox)}
                 footprint={sandbox && mapData ? storm.radius_m : null} />
        {home && (
          <>
            <MapControls viewMode={viewMode} onViewMode={setViewMode} storm={storm} showForecast={!sandbox}
                         onStorm={(st) => set({ storm: st })} forecast={mapData?.forecast} layersOn={layersOn}
                         loading={loading} onToggle={(id) => setLayersOn((l) => ({ ...l, [id]: !l[id] }))} />
            {sandbox && (
              <SandboxPanel value={storm} onChange={(st) => set({ storm: st })} onSimulateOutage={simulateOutage}
                            restoration={mapData?.restoration} />
            )}
            <Legend stormLabel={stormLabel(mapData?.storm)} />
          </>
        )}
      </div>
      <aside className="side" aria-label="Chat with the outage concierge">
        <ChatPanel messages={messages} busy={busy} onSend={send} disabled={!home} sandbox={sandbox}
                   needs={needs} onToggleNeed={(id) => set((s) => ({ needs: s.needs.includes(id) ? s.needs.filter((x) => x !== id) : [...s.needs, id] }))}
                   demoSafe={demoSafe} onDemoSafe={(v) => set({ demoSafe: v })} />
        <AuditDrawer refreshKey={messages.length} />
      </aside>
    </main>
  );
}
