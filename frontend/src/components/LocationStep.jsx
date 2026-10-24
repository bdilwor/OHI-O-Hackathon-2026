import { useState } from 'react';
import { api } from '../api.js';

const DEMO_ADDRESS = '7110 Rabbit Run, Roanoke';

export default function LocationStep({ pointResult, onConfirm }) {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const shown = pointResult || result;

  async function search(q) {
    setBusy(true); setError(''); setResult(null);
    try {
      setResult(await api.lookupAddress(q));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay">
      <div className="locate-card" role="dialog" aria-labelledby="locate-title">
        <h2 id="locate-title">Find your home</h2>
        <p>Enter your service address to see your outage risk, restoration estimate and what to do.</p>
        <form className="search-row" onSubmit={(e) => { e.preventDefault(); if (query.trim()) search(query.trim()); }}>
          <label htmlFor="addr" className="sr-only">Street address</label>
          <input id="addr" className="input" value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="House number and street, city" autoComplete="street-address" />
          <button className="btn primary" type="submit" disabled={busy || !query.trim()}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </form>
        <p className="hint">
          Or <button type="button" className="link-btn" onClick={() => { setQuery(DEMO_ADDRESS); search(DEMO_ADDRESS); }}>
            try the demo address
          </button>, or click your home on the map.
        </p>
        {error && <p className="error-text" role="alert">{error}</p>}
        {shown && !shown.found && <p className="error-text" role="alert">{shown.message}</p>}
        {shown?.found && (
          <>
            <p style={{ margin: '14px 0 0', fontWeight: 600 }}>{shown.question}</p>
            <ul className="candidates">
              {shown.candidates.map((c) => (
                <li key={c.premise} className="candidate">
                  <div>
                    <div>{c.masked_address}</div>
                    <div className="meta">{c.service_type} service</div>
                  </div>
                  <button className="btn primary small" type="button" onClick={() => onConfirm(c)}>Yes, this is my home</button>
                </li>
              ))}
            </ul>
            {shown.message && <p className="hint">{shown.message}</p>}
          </>
        )}
      </div>
    </div>
  );
}
