import { lazy, Suspense, useEffect, useState } from 'react';
import { api } from './api.js';
import LocationStep from './components/LocationStep.jsx';
import { msg } from './messages.js';
import { SANDBOX_DEFAULTS } from './components/SandboxPanel.jsx';
import { Bell, Toasts, useNotifications } from './components/Notifications.jsx';

// Heavy views (deck.gl + MapLibre) load on demand so the first screen paints fast.
const Workspace = lazy(() => import('./components/Workspace.jsx'));
const OpsDashboard = lazy(() => import('./components/OpsDashboard.jsx'));
const AboutPage = lazy(() => import('./components/AboutPage.jsx'));

const STATUS_DOT = { Calm: 'ok', Breezy: 'ok', Windy: 'live', Storm: 'out', 'Severe storm': 'out' };
const rid = () => Math.random().toString(36).slice(2, 10);
const PORTALS = {
  customer: { label: 'Customer', home: '#/', tabs: [['home', '#/', 'My home'], ['about', '#/about', 'How it works']] },
  ops: { label: 'AEP Operations', home: '#/ops', tabs: [['ops', '#/ops', 'Operations']] },
};
const PORTAL_OF = { home: 'customer', about: 'customer', ops: 'ops' };
const VIEWS = ['home', 'about', 'ops'];
const WORKSPACES = ['home', 'ops'];  // map + chat areas (each keeps its own state)
// Old Storm test / Outage test links open Operations on the matching side tab.
const route = () => {
  const h = window.location.hash;
  if (h.startsWith('#/about')) return { view: 'about' };
  if (/^#\/(ops\/)?(storm-test|sandbox)/.test(h)) return { view: 'ops', opsTab: 'storm' };
  if (/^#\/(ops\/)?outage-test/.test(h)) return { view: 'ops', opsTab: 'outage' };
  if (h.startsWith('#/ops')) return { view: 'ops' };
  return { view: 'home' };
};
const readTheme = () => {
  try { const t = localStorage.getItem('mapTheme'); if (t === 'light' || t === 'dark') return t; } catch { /* storage blocked */ }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
};

function newSpace(kind) {
  return {
    storm: kind === 'ops' ? SANDBOX_DEFAULTS : { mode: 'forecast', day: 0 },
    mapData: null, messages: [], needs: [], demoSafe: false, bottleneck: null,
    session: `${kind === 'ops' ? 'ops' : 'web'}-${rid()}`,
    outage: null, outageParams: undefined, devices: null,
  };
}

function welcome(home, kind) {
  if (home.regional) {
    return msg({ role: 'assistant', text: `Found ${home.masked_address}. This location is outside AEP's challenge data, so ` +
      'I can give a regional estimate: the live forecast here plus how similar homes in AEP\'s area fare in a storm that size. ' +
      'It\'s less certain than a home-specific answer, and I\'ll say so. Ask me anything.' });
  }
  const base = `Found your home (${home.masked_address}, ${home.service_type} service). `;
  const text = {
    ops: `Testing ${home.masked_address} (${home.service_type} service). Use the Storm tab to set a forecast day or a ` +
      'simulated storm, and the Outage test tab to place a fault (or click equipment on the map) and preview what the ' +
      'customer would see. Everything here is simulated; nothing is saved.',
  }[kind] || `${base}The map shows what the outage model predicts around you for the selected forecast day: ` +
      'green is good, red is bad. Ask me anything, or tap a suggestion below.';
  return msg({ role: 'assistant', text });
}

export default function App() {
  const [{ view, opsTab }, setRoute] = useState(route);
  const [visited, setVisited] = useState(() => new Set([route().view]));
  const [mapTheme, setMapTheme] = useState(readTheme);
  const [askHome, setAskHome] = useState(false);
  const [home, setHome] = useState(null);
  const [pointResult, setPointResult] = useState(null);
  const fresh = () => Object.fromEntries(WORKSPACES.map((k) => [k, newSpace(k)]));
  const [spaces, setSpaces] = useState(fresh);
  const [error, setError] = useState('');
  const n = useNotifications();

  useEffect(() => {
    const onHash = () => {
      const r = route();
      setRoute(r);
      setVisited((v) => (v.has(r.view) ? v : new Set(v).add(r.view)));
    };
    window.addEventListener('hashchange', onHash);
    api.health().catch(() => setError("Can't reach the outage service. Is the API running on port 8000?"));
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => { try { localStorage.setItem('mapTheme', mapTheme); } catch { /* storage blocked */ } }, [mapTheme]);

  function confirmHome(candidate) {
    setHome(candidate);
    setAskHome(false);
    setPointResult(null);
    // A new home starts fresh chats, but keeps each view's storm setting (e.g. the Ops scenario being tested).
    setSpaces((all) => Object.fromEntries(WORKSPACES.map((k) => [k, { ...newSpace(k), storm: all[k].storm,
                                                                    messages: [welcome(candidate, k)] }])));
  }

  async function lookupPoint([lon, lat]) {
    try {
      const r = await api.lookupPoint(lat, lon);
      setPointResult(r.found ? r : { ...r, point: [lon, lat] });
    } catch (e) { setError(e.message); }
  }

  const portal = PORTAL_OF[view];
  const isWorkspace = WORKSPACES.includes(view);
  const space = spaces[view];
  const spaceSetter = (k) => (fn) => setSpaces((all) => ({ ...all, [k]: fn(all[k]) }));
  const shown = isWorkspace ? space.mapData?.storm : null;
  const you = isWorkspace ? space.mapData?.your_risk : null;
  const showLocate = !home && (view === 'home' || (view === 'ops' && askHome));

  return (
    <div className={`app ${portal === 'ops' ? 'is-test is-ops' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">⚡</div>
          <div>Outage Concierge<small>AEP · HackOHI/O 2026</small></div>
        </div>
        <nav className="portal-switch" aria-label="Portal">
          {Object.entries(PORTALS).map(([k, p]) => (
            <a key={k} href={p.home} aria-current={portal === k ? 'true' : undefined}>{p.label}</a>
          ))}
        </nav>
        <nav className="tabs" aria-label={`${PORTALS[portal].label} sections`}>
          {PORTALS[portal].tabs.map(([k, href, label]) => (
            <a key={k} href={href} aria-current={view === k ? 'page' : undefined}>{label}</a>
          ))}
        </nav>
        {portal === 'ops' && <span className="internal-badge" title="Internal tools for AEP staff">Internal</span>}
        <div className="spacer" />
        {shown && (
          <span className="status-pill" title="Weather shown on the map">
            <span className={`status-dot ${shown.sandbox ? 'out' : STATUS_DOT[shown.label] || 'live'}`} />
            {shown.sandbox ? `Simulated: ${shown.label}` : `${shown.label} ${shown.when || ''}`}
          </span>
        )}
        {you?.probability != null && (
          <span className="status-pill" title={you.scope === 'regional' ? 'Regional estimate (outside AEP data)'
            : 'Chance your home loses power (model estimate)'}>
            {you.scope === 'regional' ? 'Regional outage chance' : 'Your outage chance'}:{' '}
            <b>{you.probability < 0.01 ? '<1' : Math.round(you.probability * 100)}%</b>
          </span>
        )}
        {home && isWorkspace && (
          <span className="home-chip">
            <span className="addr" title={home.masked_address}>{home.masked_address}</span>
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
        {/* Visited views stay mounted (hidden) so switching back is instant: no new map or refetch. */}
        {VIEWS.filter((k) => visited.has(k)).map((k) => (
          <div key={k} className="view" hidden={view !== k}>
            <Suspense fallback={<div className="page-loading">Loading…</div>}>
              {k === 'home' && (
                <Workspace home={home} space={spaces.home} setSpace={spaceSetter('home')} notify={n.notify}
                           onMapClick={lookupPoint} onError={setError} mapTheme={mapTheme} onMapTheme={setMapTheme}
                           active={view === 'home'} />
              )}
              {k === 'ops' && (
                <OpsDashboard home={home} space={spaces.ops} setSpace={spaceSetter('ops')} notify={n.notify}
                              onError={setError} opsTab={opsTab} onTestHome={() => { setHome(null); setAskHome(true); }}
                              mapTheme={mapTheme} onMapTheme={setMapTheme} active={view === 'ops'} />
              )}
              {k === 'about' && <AboutPage />}
            </Suspense>
          </div>
        ))}
        {error && <div className="banner" role="alert">{error} <button className="link-btn" onClick={() => setError('')}>Dismiss</button></div>}
        {showLocate && <LocationStep pointResult={pointResult} onConfirm={confirmHome} aepOnly={view !== 'home'}
                                     onCancel={view === 'ops' ? () => setAskHome(false) : undefined} />}
      </div>
    </div>
  );
}
