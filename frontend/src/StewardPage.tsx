import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ApiError,
  discardVersion,
  getVersion,
  listJobs,
  listVersions,
  publishVersion,
  retryJob,
  stewardSources,
  uploadVersion,
  type Job,
  type StewardSource,
  type Version,
  type VersionDetail,
} from "./api/client";

/**
 * The steward console: upload a new version of a source, watch it being checked in the
 * background, review what changed, then publish it (or discard it).
 *
 * Nothing a steward uploads is visible to anyone else until they press Publish: the worker
 * only prepares a version ("ready"); answers keep using the published one.
 */

type Props = { onSessionExpired: () => void };

const POLL_MS = 1500;

function message(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}

function when(iso: string | null | undefined) {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";
}

const VERSION_LABEL: Record<string, string> = {
  ready: "Ready to review",
  published: "Published",
  superseded: "Superseded",
  invalid: "Rejected",
  discarded: "Discarded",
  uploaded: "Uploaded",
  validating: "Validating",
};

export function StewardPage({ onSessionExpired }: Props) {
  const [sources, setSources] = useState<StewardSource[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onSessionExpired();
      else setError(message(e));
    },
    [onSessionExpired],
  );

  const refreshSources = useCallback(() => {
    stewardSources()
      .then((list) => {
        // Datasets first, then documents, each alphabetically by title.
        list.sort((a, b) => a.kind.localeCompare(b.kind) || a.title.localeCompare(b.title));
        setSources(list);
        setSelected((current) => current ?? list[0]?.slug ?? null);
      })
      .catch(fail);
  }, [fail]);

  useEffect(refreshSources, [refreshSources]);

  const source = sources?.find((s) => s.slug === selected) ?? null;

  return (
    <main className="page">
      <h1>Manage sources</h1>
      <p className="muted">
        Upload a new version, review it, then publish. Uploads are checked in the background, and nothing changes for
        anyone until you publish.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!sources && !error && <p className="muted">Loading sources…</p>}
      {sources && (
        <div className="steward">
          <nav className="card source-list" aria-label="Sources">
            {sources.length === 0 && <p className="muted">No sources are assigned to your groups.</p>}
            {sources.map((s) => (
              <button
                key={s.slug}
                className={`source-item${s.slug === selected ? " selected" : ""}`}
                aria-current={s.slug === selected}
                onClick={() => setSelected(s.slug)}
              >
                <span className="source-title">{s.title}</span>
                <span className="slug">
                  {s.kind} · {s.published_version ? `v${s.published_version} published` : "not published"}
                </span>
                {(s.ready_versions.length > 0 || s.active_jobs > 0) && (
                  <span className="source-flags">
                    {s.active_jobs > 0 && <span className="badge working">Processing</span>}
                    {s.ready_versions.length > 0 && (
                      <span className="badge ready">{s.ready_versions.length} to review</span>
                    )}
                  </span>
                )}
              </button>
            ))}
          </nav>
          {source && <SourcePanel key={source.slug} source={source} onChanged={refreshSources} onError={fail} />}
        </div>
      )}
    </main>
  );
}

function SourcePanel({
  source,
  onChanged,
  onError,
}: {
  source: StewardSource;
  onChanged: () => void;
  onError: (e: unknown) => void;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [preview, setPreview] = useState<VersionDetail | null>(null);
  const watching = useRef<Set<string>>(new Set()); // jobs started here: open their result when done

  const openVersion = useCallback(
    (n: number) =>
      getVersion(source.slug, n)
        .then(setPreview)
        .catch(onError),
    [source.slug, onError],
  );

  const refresh = useCallback(async () => {
    try {
      const [j, v] = await Promise.all([listJobs(source.slug), listVersions(source.slug)]);
      setJobs(j);
      setVersions(v);
      for (const job of j) {
        if (watching.current.has(job.id) && (job.status === "succeeded" || job.status === "failed")) {
          watching.current.delete(job.id);
          onChanged();
          if (job.version) openVersion(job.version);
        }
      }
    } catch (e) {
      onError(e);
    }
  }, [source.slug, onChanged, onError, openVersion]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll while something is in progress, so status changes appear without a reload.
  const busy = jobs.some((j) => j.status === "pending" || j.status === "running");
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refresh]);

  async function act(action: "publish" | "discard", n: number) {
    if (action === "discard" && !window.confirm(`Discard v${n}? Its loaded data is deleted.`)) return;
    try {
      await (action === "publish" ? publishVersion(source.slug, n) : discardVersion(source.slug, n));
      await refresh();
      onChanged();
      openVersion(n);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setPreview((p) => (p ? { ...p, issues: [e.message] } : p));
      } else onError(e);
    }
  }

  return (
    <section className="source-panel">
      <div className="card">
        <h2>
          {source.title} <span className="slug">{source.slug}</span>
        </h2>
        <UploadForm
          source={source}
          onUploaded={(job) => {
            watching.current.add(job.id);
            setPreview(null);
            refresh();
            onChanged();
          }}
          onError={onError}
        />
      </div>

      {jobs.length > 0 && (
        <div className="card">
          <h2>Recent uploads</h2>
          <ul className="jobs">
            {jobs.slice(0, 5).map((j) => (
              <JobRow
                key={j.id}
                job={j}
                onOpen={openVersion}
                onRetry={() => {
                  watching.current.add(j.id);
                  retryJob(j.id).then(refresh).catch(onError);
                }}
              />
            ))}
          </ul>
        </div>
      )}

      {preview && <Preview detail={preview} onPublish={(n) => act("publish", n)} onDiscard={(n) => act("discard", n)} />}

      <div className="card">
        <h2>Versions</h2>
        {versions === null ? (
          <p className="muted">Loading…</p>
        ) : versions.length === 0 ? (
          <p className="muted">No versions yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Status</th>
                <th scope="col">Created</th>
                <th scope="col">Size</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.version}>
                  <td>
                    <button className="link" onClick={() => openVersion(v.version)}>
                      v{v.version}
                    </button>
                  </td>
                  <td>
                    <span className={`badge status-${v.status}`}>{VERSION_LABEL[v.status] ?? v.status}</span>
                  </td>
                  <td>{when(v.created_at)}</td>
                  <td>
                    {v.row_count != null && `${v.row_count} rows`}
                    {v.chunks != null && `${v.chunks} sections`}
                    {v.status === "invalid" && `${v.issue_count} problems`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function UploadForm({
  source,
  onUploaded,
  onError,
}: {
  source: StewardSource;
  onUploaded: (job: Job) => void;
  onError: (e: unknown) => void;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [sending, setSending] = useState(false);
  const fields =
    source.kind === "dataset"
      ? [
          { name: "manifest", label: "Manifest (YAML)", accept: ".yaml,.yml" },
          { name: "data", label: "Rows (CSV)", accept: ".csv" },
        ]
      : [{ name: "document", label: "Document (Markdown)", accept: ".md,.markdown,.txt" }];

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const files: Record<string, File> = {};
    for (const f of fields) {
      const file = data.get(f.name);
      if (file instanceof File && file.size > 0) files[f.name] = file;
    }
    setSending(true);
    try {
      const job = await uploadVersion(source.slug, files);
      form.current?.reset();
      onUploaded(job);
    } catch (err) {
      onError(err);
    } finally {
      setSending(false);
    }
  }

  return (
    <form ref={form} className="upload" onSubmit={submit}>
      {fields.map((f) => (
        <label key={f.name}>
          {f.label}
          <input type="file" name={f.name} accept={f.accept} required />
        </label>
      ))}
      <button type="submit" disabled={sending}>
        {sending ? "Uploading…" : "Upload new version"}
      </button>
    </form>
  );
}

function JobRow({ job, onOpen, onRetry }: { job: Job; onOpen: (n: number) => void; onRetry: () => void }) {
  let status: React.ReactNode;
  if (job.status === "pending")
    status = <span className="badge working">{job.attempts > 0 ? "Waiting to retry" : "Waiting for the worker"}</span>;
  else if (job.status === "running")
    status = (
      <span className="badge working">
        Checking and loading{job.attempts > 1 ? ` (attempt ${job.attempts} of ${job.max_attempts})` : ""}…
      </span>
    );
  else if (job.status === "failed") status = <span className="badge status-invalid">Failed</span>;
  // What this upload produced; the version's current state is in the Versions table.
  else if (job.result_status === "ready") status = <span className="badge ready">Prepared</span>;
  else if (job.result_status === "unchanged") status = <span className="badge">Identical to v{job.version}</span>;
  else status = <span className="badge status-invalid">Rejected: {job.issue_count} problem(s)</span>;

  return (
    <li className="job">
      <div className="job-line">
        {status}
        {job.version != null && (
          <button className="link" onClick={() => onOpen(job.version!)}>
            v{job.version}
          </button>
        )}
        <span className="muted">
          {when(job.created_at)}
          {job.created_by ? ` · ${job.created_by}` : ""}
        </span>
        {job.status === "failed" && (
          <button className="secondary" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
      {job.status === "failed" && job.error && <p className="error">{job.error}</p>}
      {job.status === "pending" && job.error && <p className="muted">Last attempt: {job.error}</p>}
      {job.result_status === "invalid" && job.issues.length > 0 && (
        <ul className="issues">
          {job.issues.slice(0, 3).map((i) => (
            <li key={i}>{i}</li>
          ))}
          {job.issue_count > 3 && <li>…and {job.issue_count - 3} more (open v{job.version})</li>}
        </ul>
      )}
    </li>
  );
}

function Preview({
  detail,
  onPublish,
  onDiscard,
}: {
  detail: VersionDetail;
  onPublish: (n: number) => void;
  onDiscard: (n: number) => void;
}) {
  const v = detail.version;
  const c = detail.compared_to;
  return (
    <div className="card preview" aria-label={`Version ${v.version}`}>
      <div className="preview-head">
        <h2>
          v{v.version} <span className={`badge status-${v.status}`}>{VERSION_LABEL[v.status] ?? v.status}</span>
        </h2>
        {v.status === "ready" && (
          <div className="actions">
            <button onClick={() => onPublish(v.version)}>Publish v{v.version}</button>
            <button className="secondary" onClick={() => onDiscard(v.version)}>
              Discard
            </button>
          </div>
        )}
      </div>

      {detail.issues.length > 0 && (
        <div className="error" role="alert">
          <strong>{v.status === "invalid" ? "This upload was rejected:" : "Can't publish:"}</strong>
          <ul className="issues">
            {detail.issues.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
        </div>
      )}

      {c && (
        <p className="compare">
          Compared with published v{c.published_version}:{" "}
          {detail.kind === "dataset" ? (
            <>
              {c.published_row_count} → {v.row_count} rows
              {c.columns_added.length > 0 && <> · columns added: {c.columns_added.join(", ")}</>}
              {c.columns_removed.length > 0 && <> · columns removed: {c.columns_removed.join(", ")}</>}
            </>
          ) : (
            <>
              {c.sections_added.length ? <>sections added: {c.sections_added.join(", ")}</> : "no new sections"}
              {c.sections_removed.length > 0 && <> · sections removed: {c.sections_removed.join(", ")}</>}
            </>
          )}
        </p>
      )}

      {detail.kind === "dataset" && detail.columns.length > 0 && (
        <>
          <p className="muted">
            {v.row_count} rows. First {detail.sample_rows.length}:
          </p>
          <div className="scroll-x">
            <table className="sample">
              <thead>
                <tr>
                  {detail.columns.map((col) => (
                    <th key={col.name} scope="col" title={col.description}>
                      {col.name}
                      <span className="col-type">{col.type}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {detail.sample_rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j}>{cell ?? <span className="muted">empty</span>}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {detail.kind === "document" && detail.body && (
        <>
          <p className="muted">
            {detail.doc_title} · effective {detail.effective_date} · {v.chunks} searchable sections
          </p>
          <div className="doc-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{detail.body}</ReactMarkdown>
          </div>
        </>
      )}
    </div>
  );
}
