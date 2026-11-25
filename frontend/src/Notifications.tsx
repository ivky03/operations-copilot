import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, listNotifications, markNotificationsRead, type NotificationsList } from "./api/client";

/** Re-check for new notifications this often while the page is open. */
const POLL_MS = 60_000;

function when(iso: string) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * The bell in the top bar: how many notifications are unread, and a panel listing the latest.
 * The server only returns notifications about sources you can read right now, so this
 * component never has to decide what to hide.
 */
export function NotificationBell({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [data, setData] = useState<NotificationsList | null>(null);
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  const handle = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onSessionExpired();
    },
    [onSessionExpired],
  );
  const refresh = useCallback(() => listNotifications().then(setData).catch(handle), [handle]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => {
      if (panel.current && !panel.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const unread = data?.unread ?? 0;
  return (
    <div className="bell" ref={panel}>
      <button
        className="secondary"
        aria-expanded={open}
        aria-haspopup="true"
        onClick={() => {
          setOpen(!open);
          if (!open) refresh();
        }}
      >
        Notifications
        {unread > 0 && (
          <span className="unread" aria-label={`${unread} unread`}>
            {unread}
          </span>
        )}
      </button>
      {open && (
        <div className="bell-panel card" role="region" aria-label="Notifications">
          <div className="bell-head">
            <strong>Notifications</strong>
            {unread > 0 && (
              <button className="link" onClick={() => markNotificationsRead().then(setData).catch(handle)}>
                Mark all read
              </button>
            )}
          </div>
          {!data || data.items.length === 0 ? (
            <p className="muted small">
              Nothing yet. Follow a source under “Sources you can access” to hear when a new version is published.
            </p>
          ) : (
            <ul className="bell-list">
              {data.items.map((n) => (
                <li key={n.id} className={n.read ? "" : "is-unread"}>
                  <div>{n.message}</div>
                  <div className="muted small">{when(n.created_at)}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
