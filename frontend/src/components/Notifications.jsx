import { useCallback, useEffect, useRef, useState } from 'react';

const TOAST_MS = 7000;
const MAX_TOASTS = 4;
const ICONS = { info: 'ℹ', warning: '⚠', danger: '⚡', success: '✓' };
let nextId = 1;

/** Toasts + history. `notify(list)` accepts [{level, title, body}]. */
export function useNotifications() {
  const [toasts, setToasts] = useState([]);
  const [history, setHistory] = useState([]);
  const [unread, setUnread] = useState(0);
  const [desktop, setDesktop] = useState(false);
  const timers = useRef({});

  const dismiss = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id));
    clearTimeout(timers.current[id]);
  }, []);

  const notify = useCallback((items) => {
    if (!items?.length) return;
    const stamped = items.map((n) => ({ ...n, id: nextId++, ts: new Date() }));
    setToasts((t) => [...stamped, ...t].slice(0, MAX_TOASTS));
    setHistory((h) => [...stamped, ...h].slice(0, 50));
    setUnread((u) => u + stamped.length);
    stamped.forEach((n) => { timers.current[n.id] = setTimeout(() => dismiss(n.id), TOAST_MS); });
    if (desktop && typeof Notification !== 'undefined' && Notification.permission === 'granted' && document.hidden) {
      stamped.forEach((n) => new Notification(n.title, { body: n.body }));
    }
  }, [desktop, dismiss]);

  useEffect(() => () => Object.values(timers.current).forEach(clearTimeout), []);

  const enableDesktop = async () => {
    if (typeof Notification === 'undefined') return;
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    setDesktop(perm === 'granted');
  };

  return { toasts, history, unread, notify, dismiss, clearUnread: () => setUnread(0), desktop, enableDesktop };
}

export function Toasts({ toasts, onDismiss }) {
  return (
    <div className="toasts" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.level}`} role="status">
          <span className="toast-icon" aria-hidden="true">{ICONS[t.level] || ICONS.info}</span>
          <div>
            <b>{t.title}</b>
            <p>{t.body}</p>
          </div>
          <button type="button" className="toast-x" aria-label="Dismiss" onClick={() => onDismiss(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}

export function Bell({ history, unread, onOpen, desktop, onEnableDesktop }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="bell-wrap">
      <button type="button" className="bell" aria-label={`Notifications (${unread} new)`} aria-expanded={open}
              onClick={() => { setOpen(!open); onOpen(); }}>
        🔔{unread > 0 && <span className="badge">{unread > 9 ? '9+' : unread}</span>}
      </button>
      {open && (
        <div className="bell-panel" role="dialog" aria-label="Notification history">
          <div className="bell-head">
            <b>Notifications</b>
            {!desktop && typeof Notification !== 'undefined' && (
              <button type="button" className="link-btn" onClick={onEnableDesktop}>Enable desktop alerts</button>
            )}
          </div>
          {history.length === 0 && <p className="conf">Nothing yet. You'll be alerted when the weather or your outage risk changes.</p>}
          {history.map((n) => (
            <div key={n.id} className={`bell-item ${n.level}`}>
              <b>{n.title}</b><span>{n.ts.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span>
              <p>{n.body}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
