/**
 * The one place the web app talks to the backend.
 *
 * - Types come from schema.d.ts, generated from the backend's OpenAPI contract, so a field
 *   renamed on the server becomes a TypeScript error here instead of a blank screen.
 * - The session cookie is HttpOnly: this code never sees it. The browser attaches it to
 *   same-origin requests automatically.
 * - The CSRF token is kept in memory (not localStorage, which any script on the page could read)
 *   and sent on every request that changes data.
 */
import type { components } from "./schema";

export type User = components["schemas"]["UserOut"];
export type Session = components["schemas"]["SessionOut"];
export type SourceItem = components["schemas"]["SourceOut"];

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let csrfToken: string | null = null;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is the backend running?");
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch {
      /* non-JSON error body: keep the generic message */
    }
    throw new ApiError(response.status, message);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export async function login(email: string, password: string): Promise<Session> {
  const session = await request<Session>("POST", "/api/auth/login", { email, password });
  csrfToken = session.csrf_token;
  return session;
}

/** Restore an existing session on page load. Resolves to null when not logged in. */
export async function currentSession(): Promise<Session | null> {
  try {
    const session = await request<Session>("GET", "/api/auth/me");
    csrfToken = session.csrf_token;
    return session;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export async function logout(): Promise<void> {
  try {
    await request<void>("POST", "/api/auth/logout");
  } finally {
    csrfToken = null;
  }
}

export const listSources = () => request<SourceItem[]>("GET", "/api/sources");

export type AskResponse = components["schemas"]["AskResponse"];
export type Evidence = components["schemas"]["EvidenceOut"];
export type Passage = components["schemas"]["PassageOut"];

export type ConversationSummary = components["schemas"]["ConversationSummary"];
export type Conversation = components["schemas"]["ConversationOut"];

/** Ask a question; without a conversation id the server starts a new conversation. */
export const ask = (question: string, conversationId?: string | null) =>
  request<AskResponse>("POST", "/api/ask", {
    question,
    ...(conversationId ? { conversation_id: conversationId } : {}),
  });

export type StreamEvent =
  | { event: "step"; data: { step: string } }
  | { event: "planned"; data: { kind: string; queries: number; doc_searches: string[]; standalone_question: string | null } }
  | { event: "evidence"; data: { items: Evidence[]; notes: string[] } }
  | { event: "passages"; data: { items: Passage[] } }
  | { event: "answer"; data: AskResponse }
  | { event: "error"; data: { message: string } };

/**
 * Ask with live progress. The server sends server-sent events over a POST response
 * (EventSource only supports GET, and asking needs a body and the CSRF token), so this reads
 * the stream with fetch and splits it into events. Resolves with the final, verified answer.
 */
export async function askStream(
  question: string,
  conversationId: string | null,
  onEvent: (e: StreamEvent) => void,
): Promise<AskResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let response: Response;
  try {
    response = await fetch("/api/ask/stream", {
      method: "POST",
      headers,
      credentials: "same-origin",
      body: JSON.stringify({ question, ...(conversationId ? { conversation_id: conversationId } : {}) }),
    });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is the backend running?");
  }
  if (!response.ok || !response.body) {
    let message = `Request failed (${response.status})`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(response.status, message);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {  // events are separated by a blank line
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const name = block.match(/^event: (.*)$/m)?.[1];
      const data = block.match(/^data: (.*)$/m)?.[1];
      if (!name || data === undefined) continue;
      const event = { event: name, data: JSON.parse(data) } as StreamEvent;
      if (event.event === "answer") return event.data;
      if (event.event === "error") throw new ApiError(500, event.data.message);
      onEvent(event);
    }
  }
  throw new ApiError(0, "The connection closed before the answer arrived. Please try again.");
}

export const listConversations = () => request<ConversationSummary[]>("GET", "/api/conversations");
export const getConversation = (id: string) => request<Conversation>("GET", `/api/conversations/${id}`);
export const renameConversation = (id: string, title: string) =>
  request<ConversationSummary>("PATCH", `/api/conversations/${id}`, { title });
export const deleteConversation = (id: string) => request<void>("DELETE", `/api/conversations/${id}`);
