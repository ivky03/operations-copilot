import { type FormEvent, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, ask, type AskResponse, type Evidence } from "./api/client";

const EXAMPLES = [
  "Compare February and March net sales by country",
  "What were UK net sales in April 2026?",
  "How many desk lamps did we sell in March?",
  "What was the profit margin for each product in March?",
];

const STATUS_LABEL: Record<string, { label: string; tone: string }> = {
  answered: { label: "Answered · numbers verified", tone: "ok" },
  answered_fallback: { label: "Shown as table · summary couldn't be verified", tone: "warn" },
  clarification: { label: "Needs clarification", tone: "info" },
  unanswerable: { label: "Can't answer from available data", tone: "warn" },
  documents: { label: "Document question · not available yet", tone: "info" },
  error: { label: "Something went wrong", tone: "error" },
};

function formatCell(value: unknown, column: string, evidence: Evidence): string {
  if (value === null || value === undefined) return "–";
  if (typeof value === "number" && column === evidence.metric) {
    if (evidence.unit === "EUR") {
      const abs = Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      return `${value < 0 ? "−" : ""}€${abs}`;
    }
    if (evidence.unit === "ratio") return `${(value * 100).toFixed(2)}%`;
    return value.toLocaleString();
  }
  return String(value);
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  return (
    <section className="evidence">
      <h3>
        <span className="evidence-id">{evidence.id}</span> {evidence.title}
        {evidence.unit && <span className="muted"> ({evidence.unit})</span>}
      </h3>
      <p className="muted small">{evidence.description}</p>
      {evidence.rows.length === 0 ? (
        <p className="muted">No matching data.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {evidence.columns.map((c) => (
                  <th key={c} scope="col">
                    {c.replace(/_/g, " ")}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {evidence.rows.map((row, i) => (
                <tr key={i}>
                  {evidence.columns.map((c) => (
                    <td key={c} className={c === evidence.metric ? "num" : undefined}>
                      {formatCell(row[c], c, evidence)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small">
        Sources:{" "}
        {evidence.sources.map((s) => (
          <span key={String(s.source)} className="badge">
            {s.source} v{s.version}
          </span>
        ))}
      </p>
      <details>
        <summary>Show SQL</summary>
        <pre className="sql">{evidence.sql}</pre>
      </details>
    </section>
  );
}

export function AskPanel({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<AskResponse | null>(null);
  const [asked, setAsked] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(text: string) {
    const q = text.trim();
    if (q.length < 3 || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setAsked(q);
    try {
      setResult(await ask(q));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSessionExpired();
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submit(question);
  }

  // A clarification choice becomes a more specific question.
  function choose(option: string) {
    const refined = `${asked} (I mean ${option})`;
    setQuestion(refined);
    void submit(refined);
  }

  const status = result ? (STATUS_LABEL[result.status] ?? { label: result.status, tone: "info" }) : null;

  return (
    <section className="card ask">
      <form onSubmit={onSubmit}>
        <label htmlFor="question">Ask a question about your data</label>
        <div className="ask-row">
          <input
            id="question"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. Compare February and March net sales by country"
            maxLength={1000}
          />
          <button type="submit" disabled={busy || question.trim().length < 3}>
            {busy ? "Working…" : "Ask"}
          </button>
        </div>
      </form>
      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} type="button" className="chip" disabled={busy} onClick={() => { setQuestion(ex); void submit(ex); }}>
            {ex}
          </button>
        ))}
      </div>

      {busy && (
        <p className="muted" role="status">
          Planning the query, computing results and verifying the answer…
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {result && status && (
        <div className="answer" aria-live="polite">
          <div className="answer-head">
            <span className={`status ${status.tone}`}>{status.label}</span>
            <span className="muted small">{(result.latency_ms / 1000).toFixed(1)} s</span>
          </div>
          <div className="markdown">
            {/* react-markdown renders markdown as React elements and ignores raw HTML,
                so model-written text can't inject scripts into the page. */}
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.answer}</ReactMarkdown>
          </div>
          {result.options.length > 0 && (
            <div className="options">
              {result.options.map((o) => (
                <button key={o} type="button" className="secondary" onClick={() => choose(o)}>
                  {o}
                </button>
              ))}
            </div>
          )}
          {result.notes.map((n) => (
            <p key={n} className="note small">
              {n}
            </p>
          ))}
          {result.evidence.length > 0 && (
            <div className="evidence-list">
              <h2>Evidence</h2>
              {result.evidence.map((e) => (
                <EvidenceCard key={e.id} evidence={e} />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
