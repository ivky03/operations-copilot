/** Evidence views: computed data tables and document passage cards. */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Evidence, Passage } from "./api/client";

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

export function EvidenceCard({ evidence }: { evidence: Evidence }) {
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

/** IDs cited in the answer text: [C1], [C1, C3], [D1][C2]. */
export function citedIds(answer: string): Set<string> {
  const ids = new Set<string>();
  for (const group of answer.matchAll(/\[([^\]]*)\]/g)) {
    for (const id of group[1].matchAll(/[DC]\d+/g)) ids.add(id[0]);
  }
  return ids;
}

/** `cited` is null while the answer is still being written. */
export function PassageCard({ passage, cited }: { passage: Passage; cited: boolean | null }) {
  const state = cited === null ? "" : cited ? " cited" : " uncited";
  return (
    <section className={`evidence passage${state}`}>
      <h3>
        <span className="evidence-id">{passage.id}</span> {passage.doc_title}{" "}
        <span className="muted">› {passage.heading}</span>
      </h3>
      <p className="small">
        <span className="badge">
          {passage.source} v{passage.version}
        </span>
        {passage.current ? (
          <span className="badge ok">current</span>
        ) : (
          <span className="badge warn">superseded</span>
        )}
        {passage.effective_date && <span className="muted"> effective {passage.effective_date}</span>}
        <span className="muted">
          {" · "}
          {cited === null ? "retrieved" : cited ? "cited in the answer" : "retrieved, not cited"}
        </span>
      </p>
      <blockquote className="markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{passage.content}</ReactMarkdown>
      </blockquote>
    </section>
  );
}
