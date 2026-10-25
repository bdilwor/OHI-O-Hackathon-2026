import { useCallback, useEffect, useRef, useState } from 'react';

const TOAST_MS = 4000;
const FADE_MS = 300;
const MAX_TOASTS = 3;
const ICONS = { info: 'ℹ', warning: '⚠', danger: '⚡', success: '✓' };
let nextId = 1;

/**
 * Toasts + history. `notify(list)` accepts [{level, title, body}].
 * Toasts auto-dismiss after 4 s (paused while hovered), at most 3 show at once, and a toast with the same
 * title as one on screen replaces it instead of stacking (rapid slider changes update one toast).
 */
export function useNotifications() {
  const [toasts, setToasts] = useState([]);
  const [history, setHistory] = useState([]);
  const [unread, setUnread] = useState(0);
  const [desktop, setDesktop] = useState(false);
  const timers = useRef({});

  const remove = useCallback((id) => {
    clearTimeout(timers.current[id]);
    delete timers.current[id];
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const dismiss = useCallback((id) => { // fade out, then remove
    clearTimeout(timers.current[id]);
    setToasts((t) => t.map((x) => (x.id === id ? { ...x, leaving: true } : x)));
    timers.current[id] = setTimeout(() => remove(id), FADE_MS);
  }, [remove]);

  const schedule = useCallback((id, ms = TOAST_MS) => {
    clearTimeout(timers.current[id]);
    timers.current[id] = setTimeout(() => dismiss(id), ms);
  }, [dismiss]);

  const notify = useCallback((items) => {
    if (!items?.length) return;
    const stamped = items.map((n) => ({ ...n, id: nextId++, ts: new Date() }));
    setToasts((current) => {
      const titles = new Set(stamped.map((n) => n.title));
      const kept = current.filter((x) => !titles.has(x.title));
      current.filter((x) => titles.has(x.title)).forEach((x) => clearTimeout(timers.current[x.id]));
      const next = [...stamped, ...kept];
      next.slice(MAX_TOASTS).forEach((x) => clearTimeout(timers.current[x.id]));
      return next.slice(0, MAX_TOASTS);
    });
    stamped.forEach((n) => schedule(n.id));
    setHistory((h) => [...stamped, ...h].slice(0, 50));
    setUnread((u) => u + stamped.length);
    if (desktop && typeof Notification !== 'undefined' && Notification.permission === 'granted' && document.hidden) {
      stamped.forEach((n) => new Notification(n.title, { body: n.body }));
    }
  }, [desktop, schedule]);

  useEffect(() => () => Object.values(timers.current).forEach(clearTimeout), []);

  const enableDesktop = async () => {
    if (typeof Notification === 'undefined') return;
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    setDesktop(perm === 'granted');
  };

  return {
    toasts, history, unread, notify, dismiss, clearUnread: () => setUnread(0), desktop, enableDesktop,
    pause: (id) => clearTimeout(timers.current[id]), resume: (id) => schedule(id, 2000),
  };
}

export function Toasts({ toasts, onDismiss, onPause, onResume }) {
  return (
    <div className="toasts" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.level} ${t.leaving ? 'leaving' : ''}`} role="status"
             onMouseEnter={() => onPause?.(t.id)} onMouseLeave={() => onResume?.(t.id)}>
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
