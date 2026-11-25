/**
 * The chat: a list of your conversations on the left, the selected conversation on the right.
 *
 * Each turn shows the question, the answer, and its evidence. Follow-ups ("And April?") are
 * sent with the conversation's id, and the server uses the earlier turns to understand them.
 */
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ApiError,
  askStream,
  type AskResponse,
  type Evidence,
  type Passage,
  type StreamEvent,
  type ConversationSummary,
  deleteConversation,
  getConversation,
  listConversations,
  renameConversation,
} from "./api/client";
import { citedIds, EvidenceCard, PassageCard } from "./Evidence";

const EXAMPLES = [
  "Compare February and March net sales by country",
  "What is our return window?",
  "How much was refunded in April 2026, and how quickly must refunds be paid?",
  "How many desk lamps did we sell in March?",
  "What was the profit margin for each product in March?",
];

const STATUS_LABEL: Record<string, { label: string; tone: string }> = {
  answered: { label: "Answered · numbers and citations verified", tone: "ok" },
  answered_fallback: { label: "Evidence shown as-is · summary couldn't be verified", tone: "warn" },
  clarification: { label: "Needs clarification", tone: "info" },
  unanswerable: { label: "Can't answer from your data or documents", tone: "warn" },
  redacted: { label: "Hidden · uses a source you no longer have access to", tone: "info" },
  erased: { label: "Erased · a source it used was deleted", tone: "info" },
  error: { label: "Something went wrong", tone: "error" },
};

const STEP_LABEL: Record<string, string> = {
  planning: "Understanding the question",
  querying: "Running queries on the data",
  searching: "Searching documents you can access",
  writing: "Writing the answer",
  verifying: "Checking every number and citation",
  rewriting: "A draft failed the checks; rewriting",
};

type Progress = {
  question: string;
  steps: string[]; // in order; the last one is in progress
  understood: string | null;
  evidence: Evidence[];
  passages: Passage[];
};

function apply(progress: Progress, e: StreamEvent): Progress {
  switch (e.event) {
    case "step":
      return { ...progress, steps: [...progress.steps, e.data.step] };
    case "planned":
      return { ...progress, understood: e.data.standalone_question };
    case "evidence":
      return { ...progress, evidence: e.data.items };
    case "passages":
      return { ...progress, passages: e.data.items };
    default:
      return progress;
  }
}

/** The turn being answered: live steps, then data and passages as soon as they exist.
 *  No answer text appears here; it's only shown once verified. */
function PendingTurn({ progress }: { progress: Progress }) {
  return (
    <article className="turn">
      <p className="question">{progress.question}</p>
      {progress.understood && <p className="understood small">Understood as: “{progress.understood}”</p>}
      <ol className="steps" role="status" aria-live="polite">
        {progress.steps.map((step, i) => {
          const done = i < progress.steps.length - 1;
          return (
            <li key={`${step}-${i}`} className={done ? "done" : "active"}>
              <span aria-hidden="true">{done ? "✓" : "…"}</span> {STEP_LABEL[step] ?? step}
            </li>
          );
        })}
        {progress.steps.length === 0 && <li className="active">… Sending</li>}
      </ol>
      {progress.evidence.length > 0 && (
        <div className="evidence-list">
          <h2>Computed data</h2>
          {progress.evidence.map((e) => (
            <EvidenceCard key={e.id} evidence={e} />
          ))}
        </div>
      )}
      {progress.passages.length > 0 && (
        <div className="evidence-list">
          <h2>Document passages</h2>
          {progress.passages.map((p) => (
            <PassageCard key={p.id} passage={p} cited={null} />
          ))}
        </div>
      )}
    </article>
  );
}

function Turn({
  turn,
  latest,
  onChoose,
}: {
  turn: AskResponse;
  latest: boolean;
  onChoose: (option: string) => void;
}) {
  const status = STATUS_LABEL[turn.status] ?? { label: turn.status, tone: "info" };
  const cited = citedIds(turn.answer);
  // Cited passages first, so the reader sees what the answer used.
  const passages = [...turn.passages].sort((a, b) => Number(cited.has(b.id)) - Number(cited.has(a.id)));
  const hasEvidence = turn.evidence.length > 0 || passages.length > 0;
  return (
    <article className="turn">
      <p className="question">{turn.question}</p>
      {turn.standalone_question && (
        <p className="understood small">Understood as: “{turn.standalone_question}”</p>
      )}
      <div className="answer" aria-live={latest ? "polite" : undefined}>
        <div className="answer-head">
          <span className={`status ${status.tone}`}>{status.label}</span>
          <span className="muted small">{(turn.latency_ms / 1000).toFixed(1)} s</span>
        </div>
        <div className="markdown">
          {/* react-markdown renders markdown as React elements and ignores raw HTML,
              so model-written text can't inject scripts into the page. */}
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.answer}</ReactMarkdown>
        </div>
        {latest && turn.options.length > 0 && (
          <div className="options">
            {turn.options.map((o) => (
              <button key={o} type="button" className="secondary" onClick={() => onChoose(o)}>
                {o}
              </button>
            ))}
          </div>
        )}
        {turn.notes.map((n) => (
          <p key={n} className="note small">
            {n}
          </p>
        ))}
        {hasEvidence && (
          <details className="evidence-details" open={latest}>
            <summary>
              Evidence: {turn.evidence.length > 0 && `${turn.evidence.length} computed result(s)`}
              {turn.evidence.length > 0 && passages.length > 0 && ", "}
              {passages.length > 0 && `${passages.length} document passage(s)`}
            </summary>
            {turn.evidence.length > 0 && (
              <div className="evidence-list">
                <h2>Computed data</h2>
                {turn.evidence.map((e) => (
                  <EvidenceCard key={e.id} evidence={e} />
                ))}
              </div>
            )}
            {passages.length > 0 && (
              <div className="evidence-list">
                <h2>Document passages</h2>
                <p className="muted small">
                  Only documents you have access to are searched. Superseded versions are searched only
                  when you ask about the past.
                </p>
                {passages.map((p) => (
                  <PassageCard key={p.id} passage={p} cited={cited.has(p.id)} />
                ))}
              </div>
            )}
          </details>
        )}
      </div>
    </article>
  );
}

function ConversationList({
  items,
  selected,
  onSelect,
  onNew,
  onRename,
  onDelete,
}: {
  items: ConversationSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  function save(event: FormEvent, id: string) {
    event.preventDefault();
    if (draft.trim()) onRename(id, draft.trim());
    setEditing(null);
  }

  return (
    <nav className="chats" aria-label="Conversations">
      <button type="button" className="new-chat" onClick={onNew}>
        + New chat
      </button>
      {items.length === 0 && <p className="muted small">No conversations yet.</p>}
      <ul>
        {items.map((c) => (
          <li key={c.id} className={c.id === selected ? "selected" : undefined}>
            {editing === c.id ? (
              <form onSubmit={(e) => save(e, c.id)} className="rename">
                <input
                  aria-label="Conversation title"
                  value={draft}
                  autoFocus
                  maxLength={200}
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => setEditing(null)}
                  onKeyDown={(e) => e.key === "Escape" && setEditing(null)}
                />
              </form>
            ) : (
              <>
                <button type="button" className="chat-title" onClick={() => onSelect(c.id)} title={c.title}>
                  {c.title}
                </button>
                <span className="chat-actions">
                  <button
                    type="button"
                    className="icon"
                    aria-label={`Rename “${c.title}”`}
                    onClick={() => {
                      setDraft(c.title);
                      setEditing(c.id);
                    }}
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    className="icon"
                    aria-label={`Delete “${c.title}”`}
                    onClick={() => {
                      if (window.confirm(`Delete “${c.title}”? This can't be undone.`)) onDelete(c.id);
                    }}
                  >
                    ×
                  </button>
                </span>
              </>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function Chat({ onSessionExpired }: { onSessionExpired: () => void }) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [turns, setTurns] = useState<AskResponse[]>([]);
  const [pending, setPending] = useState<Progress | null>(null); // the turn being answered
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onSessionExpired();
      else setError(e instanceof Error ? e.message : String(e));
    },
    [onSessionExpired],
  );

  const refreshList = useCallback(() => listConversations().then(setConversations).catch(fail), [fail]);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [turns.length, pending]);

  async function open(id: string) {
    setError(null);
    try {
      const convo = await getConversation(id);
      setSelected(id);
      setTurns(convo.turns);
    } catch (e) {
      fail(e);
    }
  }

  function startNew() {
    setSelected(null);
    setTurns([]);
    setError(null);
    setQuestion("");
  }

  async function submit(text: string) {
    const q = text.trim();
    if (q.length < 3 || pending) return;
    setPending({ question: q, steps: [], understood: null, evidence: [], passages: [] });
    setError(null);
    setQuestion("");
    try {
      const result = await askStream(q, selected, (e) => setPending((p) => (p ? apply(p, e) : p)));
      setTurns((t) => [...t, result]);
      if (!selected && result.conversation_id) setSelected(result.conversation_id);
      void refreshList();
    } catch (e) {
      setQuestion(q); // give the question back so it isn't lost
      fail(e);
    } finally {
      setPending(null);
    }
  }

  async function rename(id: string, title: string) {
    try {
      await renameConversation(id, title);
      await refreshList();
    } catch (e) {
      fail(e);
    }
  }

  async function remove(id: string) {
    try {
      await deleteConversation(id);
      if (id === selected) startNew();
      await refreshList();
    } catch (e) {
      fail(e);
    }
  }

  const list = (
    <ConversationList
      items={conversations}
      selected={selected}
      onSelect={(id) => void open(id)}
      onNew={startNew}
      onRename={(id, t) => void rename(id, t)}
      onDelete={(id) => void remove(id)}
    />
  );

  return (
    <div className="chat-layout">
      <aside className="card sidebar">{list}</aside>
      <details className="card sidebar-mobile">
        <summary>Chats ({conversations.length})</summary>
        {list}
      </details>

      <section className="card thread">
        {turns.length === 0 && !pending && (
          <div className="empty">
            <h2>Ask a question about your data or policies</h2>
            <p className="muted small">
              Follow-ups work too: ask “Show net sales by country for March 2026”, then “And April?”
            </p>
            <div className="examples">
              {EXAMPLES.map((ex) => (
                <button key={ex} type="button" className="chip" onClick={() => void submit(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((t, i) => (
          <Turn key={t.run_id} turn={t} latest={i === turns.length - 1 && !pending} onChoose={(o) => void submit(o)} />
        ))}
        {pending && <PendingTurn progress={pending} />}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div ref={endRef} />
        <form
          className="ask-row composer"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(question);
          }}
        >
          <label htmlFor="question" className="visually-hidden">
            Ask a question about your data or policies
          </label>
          <input
            id="question"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={turns.length ? "Ask a follow-up, e.g. “And April?”" : "e.g. What is our return window?"}
            maxLength={1000}
          />
          <button type="submit" disabled={!!pending || question.trim().length < 3}>
            {pending ? "Working…" : "Ask"}
          </button>
        </form>
      </section>
    </div>
  );
}
