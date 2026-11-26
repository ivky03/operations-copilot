import { useEffect, useState } from "react";
import {
  ApiError,
  followSource,
  listSources,
  unfollowSource,
  type Session,
  type SourceItem,
} from "./api/client";
import { Chat } from "./Chat";
import { AdminSourcesPage } from "./AdminSourcesPage";
import { NotificationBell } from "./Notifications";
import { StewardPage } from "./StewardPage";

/** The second page each role gets besides Ask (the server enforces who may use it). */
const ROLE_PAGE: Record<string, { hash: string; label: string } | undefined> = {
  steward: { hash: "#/manage", label: "Manage sources" },
  admin: { hash: "#/admin", label: "Sources" },
};

/** The page lives in the URL hash (#/manage), so a refresh or a bookmark keeps it. */
function useView(hash: string | undefined): [boolean, (other: boolean) => void] {
  const read = () => !!hash && window.location.hash === hash;
  const [onOther, setOnOther] = useState(read);
  useEffect(() => {
    const onHash = () => setOnOther(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  });
  return [onOther, (other) => (window.location.hash = other && hash ? hash : "")];
}

type Props = { session: Session; onSignOut: () => void; onSessionExpired: () => void };

function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }) : "Not published";
}

type TableProps = { title: string; items: SourceItem[]; onFollow: (s: SourceItem) => void };

function SourceTable({ title, items, onFollow }: TableProps) {
  return (
    <section className="card">
      <h2>
        {title} <span className="count">{items.length}</span>
      </h2>
      {items.length === 0 ? (
        <p className="muted">None available to you.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Version</th>
              <th scope="col">Published</th>
              <th scope="col">
                <span className="visually-hidden">Follow</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.slug}>
                <td>
                  <div>{s.title}</div>
                  <div className="slug">{s.slug}</div>
                </td>
                <td>{s.published_version ? `v${s.published_version}` : "–"}</td>
                <td>{formatDate(s.published_at)}</td>
                <td>
                  <button
                    className={s.subscribed ? "secondary" : "secondary follow"}
                    aria-pressed={s.subscribed}
                    aria-label={`${s.subscribed ? "Unfollow" : "Follow"} ${s.title}`}
                    title={s.subscribed ? "You'll be notified of new versions" : "Be notified of new versions"}
                    onClick={() => onFollow(s)}
                  >
                    {s.subscribed ? "Following" : "Follow"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function HomePage({ session, onSignOut, onSessionExpired }: Props) {
  const [sources, setSources] = useState<SourceItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { user } = session;
  const rolePage = ROLE_PAGE[user.role];
  const [onRolePage, go] = useView(rolePage?.hash);

  useEffect(() => {
    listSources()
      .then(setSources)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onSessionExpired();
        else setError(e instanceof Error ? e.message : String(e));
      });
  }, [onSessionExpired]);

  async function toggleFollow(s: SourceItem) {
    try {
      await (s.subscribed ? unfollowSource(s.slug) : followSource(s.slug));
      setSources((all) => all?.map((x) => (x.slug === s.slug ? { ...x, subscribed: !s.subscribed } : x)) ?? null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSessionExpired();
      else setError(e instanceof Error ? e.message : String(e));
    }
  }

  const datasets = sources?.filter((s) => s.kind === "dataset") ?? [];
  const documents = sources?.filter((s) => s.kind === "document") ?? [];

  return (
    <>
      <header className="topbar">
        <span className="brand">Operations Copilot</span>
        {rolePage && (
          <nav className="views" aria-label="Pages">
            <button className={onRolePage ? "" : "current"} aria-current={!onRolePage} onClick={() => go(false)}>
              Ask
            </button>
            <button className={onRolePage ? "current" : ""} aria-current={onRolePage} onClick={() => go(true)}>
              {rolePage.label}
            </button>
          </nav>
        )}
        <div className="user">
          <NotificationBell onSessionExpired={onSessionExpired} />
          <span>
            {user.display_name} <span className="muted">· {user.role}</span>
          </span>
          <button className="secondary" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      {onRolePage && user.role === "steward" ? (
        <StewardPage onSessionExpired={onSessionExpired} />
      ) : onRolePage && user.role === "admin" ? (
        <AdminSourcesPage onSessionExpired={onSessionExpired} />
      ) : (
        <main className="page">
          <h1>Welcome, {user.display_name.split(" ")[0]}</h1>
          <p className="muted">
            Groups:{" "}
            {user.groups.length ? (
              user.groups.map((g) => (
                <span key={g} className="badge">
                  {g}
                </span>
              ))
            ) : (
              <span className="badge">none</span>
            )}
          </p>
          <Chat onSessionExpired={onSessionExpired} />

          <details className="sources-panel">
            <summary>
              Sources you can access {sources ? `(${sources.length})` : ""}
            </summary>
            <p className="muted">Sources restricted to other groups are not shown.</p>

          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {!sources && !error && <p className="muted">Loading sources…</p>}
          {sources && (
            <div className="grid">
              <SourceTable title="Datasets" items={datasets} onFollow={toggleFollow} />
              <SourceTable title="Documents" items={documents} onFollow={toggleFollow} />
            </div>
          )}
          </details>
        </main>
      )}
    </>
  );
}
