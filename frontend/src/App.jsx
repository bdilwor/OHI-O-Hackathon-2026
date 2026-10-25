import { useEffect, useState } from 'react';
import { api } from './api.js';
import LocationStep from './components/LocationStep.jsx';
import Workspace, { msg } from './components/Workspace.jsx';
import { SANDBOX_DEFAULTS } from './components/SandboxPanel.jsx';
import { Bell, Toasts, useNotifications } from './components/Notifications.jsx';

const STATUS_DOT = { Calm: 'ok', Breezy: 'ok', Windy: 'live', Storm: 'out', 'Severe storm': 'out' };
const rid = () => Math.random().toString(36).slice(2, 10);
const AREAS = [['home', '#/', 'My home'], ['sandbox', '#/storm-test', 'Storm test'], ['outage', '#/outage-test', 'Outage test']];
const route = () => {
  const h = window.location.hash;
  if (h.startsWith('#/storm-test') || h.startsWith('#/sandbox')) return 'sandbox';
  if (h.startsWith('#/outage-test')) return 'outage';
  return 'home';
};

function newSpace(kind) {
  return {
    storm: kind === 'sandbox' ? SANDBOX_DEFAULTS : { mode: 'forecast', day: 0 },
    mapData: null, messages: [], needs: [], demoSafe: false, bottleneck: null,
    session: `${{ sandbox: 'sandbox', outage: 'outage' }[kind] || 'web'}-${rid()}`,
    outage: null, outageParams: undefined, devices: null,
  };
}

function welcome(home, kind) {
  const base = `Found your home (${home.masked_address}, ${home.service_type} service). `;
  const text = {
    sandbox: `${base}This is the storm test: move the storm sliders and watch the map, notifications and my answers ` +
      'react. Everything here is simulated; nothing is saved.',
    outage: `${base}This is the outage test: pick where a fault happens (or click equipment on the map), choose a ` +
      'cause, and press Start outage to see what a customer would see. Nothing is saved.',
  }[kind] || `${base}The map shows what the outage model predicts around you for the selected forecast day: ` +
      'green is good, red is bad. Ask me anything, or tap a suggestion below.';
  return msg({ role: 'assistant', text });
}

export default function App() {
  const [view, setView] = useState(route());
  const [home, setHome] = useState(null);
  const [pointResult, setPointResult] = useState(null);
  const fresh = () => ({ home: newSpace('home'), sandbox: newSpace('sandbox'), outage: newSpace('outage') });
  const [spaces, setSpaces] = useState(fresh);
  const [error, setError] = useState('');
  const n = useNotifications();

  useEffect(() => {
    const onHash = () => setView(route());
    window.addEventListener('hashchange', onHash);
    api.health().catch(() => setError("Can't reach the outage service. Is the API running on port 8000?"));
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  function confirmHome(candidate) {
    setHome(candidate);
    setPointResult(null);
    setSpaces(Object.fromEntries(AREAS.map(([k]) => [k, { ...newSpace(k), messages: [welcome(candidate, k)] }])));
  }

  async function lookupPoint([lon, lat]) {
    try { setPointResult(await api.lookupPoint(lat, lon)); } catch (e) { setError(e.message); }
  }

  const space = spaces[view];
  const setSpace = (fn) => setSpaces((all) => ({ ...all, [view]: fn(all[view]) }));
  const shown = space.mapData?.storm;
  const you = space.mapData?.your_risk;

  return (
    <div className={`app ${view !== 'home' ? 'is-test' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">⚡</div>
          <div>Outage Concierge<small>AEP · HackOHI/O 2026</small></div>
        </div>
        <nav className="tabs" aria-label="Areas">
          {AREAS.map(([k, href, label]) => (
            <a key={k} href={href} aria-current={view === k ? 'page' : undefined}>{label}</a>
          ))}
        </nav>
        <div className="spacer" />
        {shown && (
          <span className="status-pill" title="Weather shown on the map">
            <span className={`status-dot ${shown.sandbox ? 'out' : STATUS_DOT[shown.label] || 'live'}`} />
            {shown.sandbox ? `Simulated: ${shown.label}` : `${shown.label} ${shown.when || ''}`}
          </span>
        )}
        {you?.probability != null && (
          <span className="status-pill" title="Chance your home loses power (model estimate)">
            Your outage chance: <b>{you.probability < 0.01 ? '<1' : Math.round(you.probability * 100)}%</b>
          </span>
        )}
        {home && (
          <span className="home-chip">
            {home.masked_address}
            <button type="button" onClick={() => { setHome(null); setSpaces(fresh()); }}>
              Change
            </button>
          </span>
        )}
        <Bell history={n.history} unread={n.unread} onOpen={n.clearUnread} desktop={n.desktop}
              onEnableDesktop={n.enableDesktop} />
      </header>
      <Toasts toasts={n.toasts} onDismiss={n.dismiss} onPause={n.pause} onResume={n.resume} />
      <div className="workspace-wrap">
        <Workspace key={view} kind={view} home={home} space={space} setSpace={setSpace} notify={n.notify}
                   onMapClick={lookupPoint} onError={setError} />
        {error && <div className="banner" role="alert">{error} <button className="link-btn" onClick={() => setError('')}>Dismiss</button></div>}
        {!home && <LocationStep pointResult={pointResult} onConfirm={confirmHome} />}
      </div>
    </div>
  );
}
