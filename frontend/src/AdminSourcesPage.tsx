import { useCallback, useEffect, useState } from "react";
import {
  adminGroups,
  adminSources,
  ApiError,
  registerSource,
  type AdminSource,
  type Group,
} from "./api/client";

/**
 * Administrators register new sources and decide which groups may read them. They see every
 * source's name and readers, but never its content (admins have no data access). A steward in
 * one of the chosen groups then uploads the first version under Manage sources.
 */

type Props = { onSessionExpired: () => void };

const DATASET_NAME = /^[a-z][a-z0-9_]{0,62}$/;
const DOCUMENT_NAME = /^[a-z][a-z0-9-]{0,99}$/;

export function AdminSourcesPage({ onSessionExpired }: Props) {
  const [sources, setSources] = useState<AdminSource[] | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onSessionExpired();
      else setError(e instanceof Error ? e.message : String(e));
    },
    [onSessionExpired],
  );

  const refresh = useCallback(() => {
    adminSources().then(setSources).catch(fail);
  }, [fail]);

  useEffect(() => {
    refresh();
    adminGroups().then(setGroups).catch(fail);
  }, [refresh, fail]);

  return (
    <main className="page">
      <h1>Sources</h1>
      <p className="muted">
        Register a dataset or document and choose who can read it. A steward in one of those groups then uploads its
        first version.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <RegisterForm
        groups={groups}
        onRegistered={(slug) => {
          setError(null);
          setCreated(slug);
          refresh();
        }}
        onError={fail}
      />
      {created && (
        <p className="banner" role="status">
          Registered <strong>{created}</strong>. It has no content until a steward uploads and publishes a version.
        </p>
      )}
      <section className="card">
        <h2>
          All sources <span className="count">{sources?.length ?? ""}</span>
        </h2>
        {!sources ? (
          <p className="muted">Loading…</p>
        ) : (
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Kind</th>
                  <th scope="col">Readers</th>
                  <th scope="col">Published</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.slug} className={s.slug === created ? "highlight" : ""}>
                    <td>
                      <div>{s.title}</div>
                      <div className="slug">{s.slug}</div>
                    </td>
                    <td>{s.kind}</td>
                    <td>
                      {s.groups.map((g) => (
                        <span key={g} className="badge">
                          {g}
                        </span>
                      ))}
                    </td>
                    <td>{s.published_version ? `v${s.published_version}` : <span className="muted">not yet</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}

function RegisterForm({
  groups,
  onRegistered,
  onError,
}: {
  groups: Group[];
  onRegistered: (slug: string) => void;
  onError: (e: unknown) => void;
}) {
  const [kind, setKind] = useState<"dataset" | "document">("dataset");
  const [slug, setSlug] = useState("");
  const [title, setTitle] = useState("");
  const [readers, setReaders] = useState<string[]>([]);
  const [sending, setSending] = useState(false);

  const pattern = kind === "dataset" ? DATASET_NAME : DOCUMENT_NAME;
  const nameOk = pattern.test(slug);
  const ready = nameOk && title.trim() && readers.length > 0;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSending(true);
    try {
      const source = await registerSource({ slug, kind, title: title.trim(), groups: readers });
      setSlug("");
      setTitle("");
      setReaders([]);
      onRegistered(source.slug);
    } catch (err) {
      onError(err);
    } finally {
      setSending(false);
    }
  }

  return (
    <form className="card register" onSubmit={submit}>
      <h2>Register a source</h2>
      <fieldset className="choice">
        <legend>Kind</legend>
        {(["dataset", "document"] as const).map((k) => (
          <label key={k}>
            <input type="radio" name="kind" checked={kind === k} onChange={() => setKind(k)} /> {k}
          </label>
        ))}
      </fieldset>
      <label htmlFor="source-name">Name</label>
      <input
        id="source-name"
        value={slug}
        onChange={(e) => setSlug(e.target.value.trim())}
        placeholder={kind === "dataset" ? "purchase_orders" : "supplier-code-of-conduct"}
        aria-describedby="name-hint"
      />
      <span id="name-hint" className={`hint${slug && !nameOk ? " bad" : ""}`}>
        {kind === "dataset"
          ? "Lowercase letters, digits and underscores. Questions refer to it as name.column."
          : "Lowercase letters, digits and hyphens."}
      </span>
      <label htmlFor="source-title">Title</label>
      <input id="source-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Supplier invoices" />
      <fieldset className="choice">
        <legend>Who can read it</legend>
        {groups.map((g) => (
          <label key={g.slug} title={g.name}>
            <input
              type="checkbox"
              checked={readers.includes(g.slug)}
              onChange={(e) =>
                setReaders((r) => (e.target.checked ? [...r, g.slug] : r.filter((x) => x !== g.slug)))
              }
            />{" "}
            {g.slug}
          </label>
        ))}
      </fieldset>
      <button type="submit" disabled={!ready || sending}>
        {sending ? "Registering…" : "Register"}
      </button>
    </form>
  );
}
