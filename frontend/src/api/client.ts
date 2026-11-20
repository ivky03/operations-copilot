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

export const listConversations = () => request<ConversationSummary[]>("GET", "/api/conversations");
export const getConversation = (id: string) => request<Conversation>("GET", `/api/conversations/${id}`);
export const renameConversation = (id: string, title: string) =>
  request<ConversationSummary>("PATCH", `/api/conversations/${id}`, { title });
export const deleteConversation = (id: string) => request<void>("DELETE", `/api/conversations/${id}`);
