// Thin client for the FastAPI backend (proxied at /api by Vite).
const BASE = '/api';

async function request(path, { method = 'GET', body, params } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  Object.entries(params || {}).forEach(([k, v]) => v !== undefined && v !== null && url.searchParams.set(k, v));
  const res = await fetch(url, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

export const api = {
  health: () => request('/health'),
  lookupAddress: (address) => request('/lookup', { params: { address } }),
  lookupPoint: (lat, lon) => request('/lookup', { params: { lat, lon } }),
  mapContext: (premise, storm) => request('/map/context', {
    params: storm?.mode === 'sandbox'
      ? { premise, mode: 'sandbox', intensity: storm.intensity, crews: storm.crews, duration_days: storm.duration_days,
          radius_m: storm.radius_m }
      : { premise, day: storm?.day ?? 0 },
  }),
  stormEventsUrl: '/api/storm/events',
  chat: (body) => request('/chat', { method: 'POST', body }),
  outageCurrent: (premise) => request('/outages/current', { params: { premise } }),
  outageDevices: (premise) => request('/outage-test/devices', { params: { premise } }),
  outageTest: (premise, o) => request('/outage-test', {
    params: { premise, cause: o.cause, reported_hours_ago: o.reported_hours_ago, crews: o.crews,
              ...(o.point ? { lat: o.point[1], lon: o.point[0] } : { device: o.device }) },
  }),
  forecast: (premise) => request('/forecast', { params: { premise } }),
  audit: (limit = 30) => request('/audit', { params: { limit } }),
  prepGuidance: (premise, needs, storm) => request('/tools/prep_guidance', {
    method: 'POST', body: { premise, declared_needs: needs, storm: stormBody(storm) } }),
  alertEligibility: (premise, needs, storm) => request('/tools/check_alert_eligibility', {
    method: 'POST', body: { premise, declared_needs: needs, storm: stormBody(storm) } }),
  draftMessage: (premise, kind) => request('/tools/draft_message', { method: 'POST', body: { premise, kind } }),
  opsOverview: (sel) => request('/ops/overview', {
    params: sel.mode === 'sandbox' ? { mode: 'sandbox', intensity: sel.intensity, duration_days: sel.duration_days, crews: sel.crews }
      : { mode: 'forecast', day: sel.day, crews: sel.crews } }),
  regionalLocate: ({ address, lat, lon }) => request('/regional/locate', { params: { address, lat, lon } }),
};

// The storm setting as the tools expect it (forecast day or sandbox sliders).
function stormBody(storm) {
  if (!storm) return undefined;
  return storm.mode === 'sandbox'
    ? { mode: 'sandbox', intensity: storm.intensity, crews: storm.crews, duration_days: storm.duration_days, radius_m: storm.radius_m }
    : { mode: 'forecast', day: storm.day ?? 0 };
}
