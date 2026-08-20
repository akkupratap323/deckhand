/**
 * The stage between uploading a deck and presenting it.
 *
 * One route covers both halves because they are one continuous experience:
 * while the deck ingests this shows the live parsing view, and the moment it
 * is ready the same page becomes the review gallery. Routing them separately
 * would put a navigation in the middle of a process the user is watching.
 *
 * Polling stops as soon as nothing is in flight. A deck that arrives already
 * finished never starts a timer at all.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { getDeck, getDeckStatus } from "../api";
import { Gallery } from "../components/Gallery";
import { Parsing } from "../components/Parsing";
import type { DeckDetail, DeckStatus } from "../types";

/**
 * Poll intervals, per phase.
 *
 * Rendering is fast - measured at ~46ms per page, so a 30-page deck is done in
 * under a second and a half. Polling that at the narration rate would show two
 * jumps instead of a fill. Narration is the opposite: several seconds per
 * slide, where frequent polling buys nothing and just makes requests.
 */
const POLL_FAST_MS = 300;
const POLL_SLOW_MS = 900;

interface Props {
  deckId: string;
}

export function DeckPage({ deckId }: Props) {
  const [status, setStatus] = useState<DeckStatus | null>(null);
  const [deck, setDeck] = useState<DeckDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(1);

  // Which deck the loaded detail belongs to, so a slow response for a deck we
  // have already navigated away from cannot overwrite the current one.
  const loadedFor = useRef<string | null>(null);

  const poll = useCallback(async () => {
    try {
      const next = await getDeckStatus(deckId);
      setStatus(next);
      setError(null);

      // Slides exist from "ready" onwards. Fetch them once, then let the
      // synthesis phase finish quietly in the background.
      if (next.status === "ready" && loadedFor.current !== deckId) {
        loadedFor.current = deckId;
        const detail = await getDeck(deckId);
        setDeck(detail);
      }
      return next;
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not reach the backend.",
      );
      return null;
    }
  }, [deckId]);

  // No reset of deck/status/selected here: App keys this component by deck id,
  // so a different deck is a fresh mount with fresh state rather than a
  // re-render that would need clearing.
  useEffect(() => {
    void poll();
  }, [poll]);

  // Keep polling until ingestion is fully finished - "ready" is not the end,
  // since synthesis still runs after it and adds the transitions.
  const settled = status?.status === "failed" || status?.phase === "done";
  const phase = status?.phase;
  useEffect(() => {
    if (!status || settled) return;
    const every =
      phase === "rendering" || phase === "queued" || phase === "converting"
        ? POLL_FAST_MS
        : POLL_SLOW_MS;
    const timer = window.setInterval(() => void poll(), every);
    return () => window.clearInterval(timer);
    // `status` is in the deps so the first poll result starts the timer; the
    // interval then re-arms whenever the phase changes pace.
  }, [status, settled, phase, poll]);

  const start = useCallback(() => {
    window.location.hash = `#/present/${encodeURIComponent(deckId)}`;
  }, [deckId]);

  const goHome = () => {
    window.location.hash = "#/";
  };

  return (
    <div className="app">
      <header className="header header--sub">
        <button type="button" className="btn btn--ghost" onClick={goHome}>
          ← Library
        </button>
        <div className="header__spacer" />
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {status?.status === "failed" && (
        <section className="panel panel--bad">
          <h2>That deck could not be processed</h2>
          <p>{status.error ?? "Ingestion failed."}</p>
          <button type="button" className="btn" onClick={goHome}>
            Back to library
          </button>
        </section>
      )}

      {status && status.status === "processing" && (
        <Parsing deckId={deckId} status={status} />
      )}

      {status && status.status === "ready" && deck && (
        <Gallery
          deck={deck}
          selected={selected}
          onSelect={setSelected}
          onStart={start}
          finishing={status.phase !== "done"}
        />
      )}

      {!status && !error && <p className="empty">Loading…</p>}
    </div>
  );
}
