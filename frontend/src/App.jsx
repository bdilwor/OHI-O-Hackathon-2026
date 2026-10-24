import { useEffect, useState } from 'react';
import { api } from './api.js';
import LocationStep from './components/LocationStep.jsx';
import Workspace, { msg } from './components/Workspace.jsx';
import { SANDBOX_DEFAULTS } from './components/SandboxPanel.jsx';
import { Bell, Toasts, useNotifications } from './components/Notifications.jsx';

const STATUS_DOT = { Calm: 'ok', Breezy: 'ok', Windy: 'live', Storm: 'out', 'Severe storm': 'out' };
const rid = () => Math.random().toString(36).slice(2, 10);
const route = () => (window.location.hash.startsWith('#/sandbox') ? 'sandbox' : 'home');

function newSpace(kind) {
  return {
    storm: kind === 'sandbox' ? SANDBOX_DEFAULTS : { mode: 'forecast', day: 0 },
    mapData: null, messages: [], needs: [], demoSafe: false, bottleneck: null,
    session: `${kind === 'sandbox' ? 'sandbox' : 'web'}-${rid()}`,
  };
}

function welcome(home, kind) {
  const base = `Found your home (${home.masked_address}, ${home.service_type} service). `;
  return msg({ role: 'assistant', text: kind === 'sandbox'
    ? `${base}This is the sandbox: move the storm sliders and watch the map, notifications and my answers react. ` +
      'Everything here is simulated; nothing is saved.'
    : `${base}The map shows what the outage model predicts around you for the selected forecast day: green is good, ` +
      'red is bad. Ask me anything, or tap a suggestion below.' });
}

export default function App() {
  const [view, setView] = useState(route());
  const [home, setHome] = useState(null);
  const [pointResult, setPointResult] = useState(null);
  const [spaces, setSpaces] = useState({ home: newSpace('home'), sandbox: newSpace('sandbox') });
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
    setSpaces({
      home: { ...newSpace('home'), messages: [welcome(candidate, 'home')] },
      sandbox: { ...newSpace('sandbox'), messages: [welcome(candidate, 'sandbox')] },
    });
  }

  async function lookupPoint([lon, lat]) {
    try { setPointResult(await api.lookupPoint(lat, lon)); } catch (e) { setError(e.message); }
  }

  const space = spaces[view];
  const setSpace = (fn) => setSpaces((all) => ({ ...all, [view]: fn(all[view]) }));
  const shown = space.mapData?.storm;
  const you = space.mapData?.your_risk;

  return (
    <div className={`app ${view === 'sandbox' ? 'is-sandbox' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">⚡</div>
          <div>Outage Concierge<small>AEP · HackOHI/O 2026</small></div>
        </div>
        <nav className="tabs" aria-label="Areas">
          <a href="#/" aria-current={view === 'home' ? 'page' : undefined}>My home</a>
          <a href="#/sandbox" aria-current={view === 'sandbox' ? 'page' : undefined}>
            Sandbox <span className="tag sandbox">demo</span>
          </a>
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
            <button type="button" onClick={() => { setHome(null); setSpaces({ home: newSpace('home'), sandbox: newSpace('sandbox') }); }}>
              Change
            </button>
          </span>
        )}
        <Bell history={n.history} unread={n.unread} onOpen={n.clearUnread} desktop={n.desktop}
              onEnableDesktop={n.enableDesktop} />
      </header>
      {view === 'sandbox' && (
        <div className="sandbox-banner" role="note">
          <b>Sandbox</b> · simulated storms for testing and demos. Uses the real outage model, but nothing is saved or
          sent to AEP systems.
        </div>
      )}
      <Toasts toasts={n.toasts} onDismiss={n.dismiss} />
      <div className="workspace-wrap">
        <Workspace key={view} kind={view} home={home} space={space} setSpace={setSpace} notify={n.notify}
                   onMapClick={lookupPoint} onError={setError} />
        {error && <div className="banner" role="alert">{error} <button className="link-btn" onClick={() => setError('')}>Dismiss</button></div>}
        {!home && <LocationStep pointResult={pointResult} onConfirm={confirmHome} />}
      </div>
    </div>
  );
}
