import { useEffect, useRef, useState } from 'react';
import ConfidenceMeter from './ConfidenceMeter.jsx';
import { renderReply } from '../format.js';

const SUGGESTIONS = ['Will I lose power?', 'How long until my power is back?', 'What should I do to prepare?',
  'What if crews are delayed until tomorrow?', 'Do I qualify for outage alerts?'];
const OPS_SUGGESTIONS = ['What is the outage risk for this premise?', 'How long would restoration take?',
  'What should the customer do to prepare?', 'What if crews are delayed until tomorrow?',
  'Does the customer qualify for alerts?'];
const CHANGE_TITLE = { risk: 'What could change these odds', restoration: 'What could change this repair time' };

export const NEEDS = [
  { id: 'medical_device', label: 'Powered medical device' },
  { id: 'refrigerated_meds', label: 'Refrigerated medicine' },
  { id: 'well_pump', label: 'Well pump' },
  { id: 'sump_pump', label: 'Sump pump' },
  { id: 'mobility', label: 'Mobility needs' },
];

function Message({ m }) {
  if (m.role === 'user') return <div className="msg user">{m.text}</div>;
  return (
    <div className={`msg assistant ${m.proactive ? 'proactive' : ''}`}>
      <div className="bubble">
        {renderReply(m.text)}
        {m.storm?.hypothetical && <div className="tag demo" style={{ display: 'inline-block' }}>hypothetical storm</div>}
        {m.changers?.length > 0 && CHANGE_TITLE[m.qtype] && (
          <div className="changers">
            <span>{CHANGE_TITLE[m.qtype]}</span>
            <ul>{m.changers.map((c) => <li key={c}>{c}</li>)}</ul>
          </div>
        )}
        <ConfidenceMeter confidence={m.confidence} />
      </div>
      {(m.mode || m.tools?.length > 0) && (
        <div className="meta">
          {m.mode && <span>{m.mode === 'llm' ? `AI (${m.provider})` : m.mode === 'demo_cache' ? 'demo-safe cache' : 'rules-based'}</span>}
          {m.tools?.length > 0 && <span>tools: {m.tools.join(', ')}</span>}
          {m.fallback && <span>fallback</span>}
        </div>
      )}
    </div>
  );
}

const AREA = {
  home: ['customer', 'Answers come from AEP data, the outage model and the live forecast for the day shown on the map.'],
  sandbox: ['AEP Operations · storm test', 'Staff briefings from the real outage model under the simulated storm settings.'],
  outage: ['AEP Operations · outage test', 'Staff briefings from the real model and grid map for the simulated outage.'],
  ops: ['AEP Operations', 'Staff briefings from the real outage model and grid map under the scenario and outage set here.'],
};

export default function ChatPanel({ messages, busy, onSend, needs, onToggleNeed, demoSafe, onDemoSafe, disabled,
                                    area = 'home' }) {
  const [tag, blurb] = AREA[area] || AREA.home;
  const ops = area !== 'home';
  const [text, setText] = useState('');
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages, busy]);

  const send = (t) => { if (t.trim() && !busy) { onSend(t.trim()); setText(''); } };

  return (
    <>
      <div className="chat-head">
        <h2>Outage Concierge <span className={`tag ${ops ? 'sandbox' : 'customer'}`}>{tag}</span></h2>
        <p>{blurb}</p>
        <div className="needs" role="group" aria-label={ops ? 'Customer-declared needs (optional)' : 'Tell us about needs at home (optional)'}>
          {NEEDS.map((n) => (
            <button key={n.id} type="button" className="need" aria-pressed={needs.includes(n.id)}
                    onClick={() => onToggleNeed(n.id)}>{n.label}</button>
          ))}
        </div>
      </div>
      <div className="messages" aria-live="polite">
        {messages.length === 0 && (
          <p className="conf">{disabled ? 'Find your home on the map to start. Your answers will appear here.' : ''}</p>
        )}
        {messages.map((m) => <Message key={m.id} m={m} />)}
        {busy && <div className="typing">Checking AEP data…</div>}
        <div ref={endRef} />
      </div>
      <div className="chips">
        {(ops ? OPS_SUGGESTIONS : SUGGESTIONS).map((s) => (
          <button key={s} type="button" className="chip" disabled={disabled || busy} onClick={() => send(s)}>{s}</button>
        ))}
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(text); }}>
        <label htmlFor="chat-input" className="sr-only">Ask a question</label>
        <input id="chat-input" className="input" value={text} onChange={(e) => setText(e.target.value)}
               placeholder={disabled ? 'Find your home first' : ops ? 'Ask about this location…' : 'Ask about your power…'} disabled={disabled} />
        <button className="btn primary" type="submit" disabled={disabled || busy || !text.trim()}>Send</button>
      </form>
      <div className="composer-foot">
        <label className="toggle-row">
          <input type="checkbox" checked={demoSafe} onChange={(e) => onDemoSafe(e.target.checked)} />
          Demo-safe mode (cached answers)
        </label>
      </div>
    </>
  );
}
