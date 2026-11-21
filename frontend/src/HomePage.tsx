import { useEffect, useState } from "react";
import { ApiError, listSources, type Session, type SourceItem } from "./api/client";
import { Chat } from "./Chat";

type Props = { session: Session; onSignOut: () => void; onSessionExpired: () => void };

function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }) : "Not published";
}

function SourceTable({ title, items }: { title: string; items: SourceItem[] }) {
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

  useEffect(() => {
    listSources()
      .then(setSources)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) onSessionExpired();
        else setError(e instanceof Error ? e.message : String(e));
      });
  }, [onSessionExpired]);

  const datasets = sources?.filter((s) => s.kind === "dataset") ?? [];
  const documents = sources?.filter((s) => s.kind === "document") ?? [];

  return (
    <>
      <header className="topbar">
        <span className="brand">Operations Copilot</span>
        <div className="user">
          <span>
            {user.display_name} <span className="muted">· {user.role}</span>
          </span>
          <button className="secondary" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

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
            <SourceTable title="Datasets" items={datasets} />
            <SourceTable title="Documents" items={documents} />
          </div>
        )}
        </details>
      </main>
    </>
  );
}
