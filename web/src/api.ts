/** Thin client over the deck API. The backend owns all of this state. */

import type {
  DeckDetail,
  DeckStatus,
  DeckSummary,
  Health,
  Presenter,
  SessionInsights,
  TranscriptTurn,
} from "./types";

/**
 * Backend origin.
 *
 * Empty string means *same origin*, which is correct in both modes: the
 * container serves the API and the app from one process, and in development
 * Vite proxies /api through to the backend (see vite.config.ts).
 *
 * Do not default this to "http://localhost:7860". On Windows `localhost`
 * resolves to ::1 first and uvicorn on 0.0.0.0 is IPv4-only, so the browser
 * gets connection refused even though curl works.
 */
export const SERVER_URL = import.meta.env.VITE_SERVER_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${SERVER_URL}${path}`, init);

  if (!response.ok) {
    // FastAPI puts the human-readable reason in `detail`.
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body; keep the status-code message */
    }
    throw new Error(detail);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listDecks(): Promise<{ decks: DeckSummary[] }> {
  return request("/api/decks");
}

export function getDeck(id: string): Promise<DeckDetail> {
  return request(`/api/decks/${id}`);
}

export function getDeckStatus(id: string): Promise<DeckStatus> {
  return request(`/api/decks/${id}/status`);
}

export function uploadDeck(file: File): Promise<DeckSummary> {
  const body = new FormData();
  body.append("file", file);
  return request("/api/decks", { method: "POST", body });
}

export function deleteDeck(id: string): Promise<void> {
  return request(`/api/decks/${id}`, { method: "DELETE" });
}

/**
 * Analyse a finished conversation.
 *
 * The transcript is sent from here rather than read server-side because the
 * server keeps none: the pipeline discards its LLM context when the session
 * ends, so the browser holds the only full record of what was said.
 */
export function getInsights(
  deckId: string,
  turns: TranscriptTurn[],
): Promise<SessionInsights> {
  return request("/api/insights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ deck_id: deckId, turns }),
  });
}

export function listPresenters(): Promise<{
  presenters: Presenter[];
  default: string;
}> {
  return request("/api/presenters");
}

export function getHealth(): Promise<Health> {
  return request("/api/health");
}

/**
 * URL of a slide's rendered page image.
 *
 * Not fetched through `request` - this goes straight into an <img src>, so the
 * browser handles caching and decoding. The backend serves these immutable, so
 * no cache-busting parameter is wanted: during ingestion the same URL becomes
 * valid the moment its page finishes rendering, and a stale 404 is retried by
 * remounting the image rather than by changing the URL.
 */
export function slideImageUrl(deckId: string, slideNumber: number): string {
  return `${SERVER_URL}/api/decks/${deckId}/slides/${slideNumber}/image`;
}
