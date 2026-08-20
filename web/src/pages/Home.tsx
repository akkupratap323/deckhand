/**
 * The library: upload a deck, see everything you have, open one.
 *
 * Uploading navigates straight to the deck page rather than leaving you on a
 * list watching a row tick over - the interesting part of ingestion is the
 * parsing view, and this is the only moment it is worth watching.
 *
 * The list still shows progress for anything ingesting in the background
 * (uploaded, then navigated away from), so it polls while any deck is
 * unfinished and stops as soon as none are.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { deleteDeck, getHealth, listDecks, uploadDeck } from "../api";
import { SlideImage } from "../components/SlideImage";
import type { DeckSummary, Health } from "../types";

const POLL_MS = 1500;

function relativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

type SortKey = "recent" | "title" | "slides";

export function Home() {
  const [decks, setDecks] = useState<DeckSummary[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("recent");

  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const { decks: fetched } = await listDecks();
      setDecks(fetched);
      setError(null);
      return fetched;
    } catch (err) {
      setError(
        err instanceof Error
          ? `${err.message} — is the backend running?`
          : "Could not reach the backend.",
      );
      return [];
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Capability, not state: this tells the drop zone whether to advertise
    // PowerPoint at all. A failure here is not worth surfacing - the upload
    // endpoint refuses unsupported files with a clear message anyway.
    void getHealth().then(setHealth).catch(() => undefined);
  }, [refresh]);

  const anyProcessing = decks.some((d) => d.status === "processing");
  useEffect(() => {
    if (!anyProcessing) return;
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [anyProcessing, refresh]);

  const handleFiles = useCallback(async (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;

    setUploading(true);
    setError(null);
    try {
      const created = await uploadDeck(file);
      // Straight into the parsing view - that is the thing worth seeing.
      window.location.hash = `#/deck/${encodeURIComponent(created.id)}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }, []);

  const handleDelete = useCallback(
    async (deck: DeckSummary) => {
      if (!window.confirm(`Delete "${deck.title}"? This cannot be undone.`)) return;
      try {
        await deleteDeck(deck.id);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not delete that deck.");
      }
    },
    [refresh],
  );

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? decks.filter(
          (d) =>
            d.title.toLowerCase().includes(needle) ||
            (d.source ?? "").toLowerCase().includes(needle),
        )
      : decks;

    const sorted = [...filtered];
    switch (sort) {
      case "title":
        sorted.sort((a, b) => a.title.localeCompare(b.title));
        break;
      case "slides":
        sorted.sort((a, b) => b.slide_count - a.slide_count);
        break;
      default:
        sorted.sort(
          (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        );
    }
    return sorted;
  }, [decks, query, sort]);

  const accept = health?.pptx_supported
    ? "application/pdf,.pdf,.pptx,.ppt"
    : "application/pdf,.pdf";

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1 className="header__title">Voice Presentation Agent</h1>
          <p className="header__sub">
            Upload a deck and let the agent present it aloud — interrupt any time to
            ask questions.
          </p>
        </div>
        {health && (
          <div className="caps" title="Server capabilities">
            <span className="caps__item" data-on={health.pptx_supported}>
              {health.pptx_supported ? "PowerPoint ready" : "PDF only"}
            </span>
            <span className="caps__item" data-on={health.vision_narration}>
              {health.vision_narration ? "Vision narration" : "Text narration"}
            </span>
          </div>
        )}
      </header>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <section
        className={`drop${dragging ? " drop--over" : ""}${uploading ? " drop--busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void handleFiles(e.dataTransfer.files);
        }}
      >
        <input
          ref={fileInput}
          type="file"
          accept={accept}
          className="drop__input"
          id="deck-file"
          onChange={(e) => void handleFiles(e.target.files)}
          disabled={uploading}
        />
        <label htmlFor="deck-file" className="drop__label">
          <span className="drop__icon" aria-hidden="true">
            ↑
          </span>
          <span className="drop__headline">
            {uploading
              ? "Uploading…"
              : health?.pptx_supported
                ? "Drop a PDF or PowerPoint here, or choose a file"
                : "Drop a PDF here, or choose a file"}
          </span>
          <span className="drop__hint">
            {health?.pptx_supported
              ? "PDF, PPTX and PPT. PowerPoint is converted before parsing."
              : "PDF only — install LibreOffice on the server to enable PowerPoint."}
          </span>
        </label>
      </section>

      <section className="library">
        <div className="library__bar">
          <h2 className="library__heading">
            Decks
            {decks.length > 0 && <span className="library__count">{decks.length}</span>}
          </h2>
          {decks.length > 1 && (
            <div className="library__tools">
              <input
                type="search"
                className="input"
                placeholder="Search decks…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Search decks"
              />
              <select
                className="input input--select"
                value={sort}
                onChange={(e) => setSort(e.target.value as SortKey)}
                aria-label="Sort decks"
              >
                <option value="recent">Newest</option>
                <option value="title">Title</option>
                <option value="slides">Most slides</option>
              </select>
            </div>
          )}
        </div>

        {loading && <p className="library__empty">Loading…</p>}
        {!loading && decks.length === 0 && (
          <p className="library__empty">No decks yet. Upload one above to get started.</p>
        )}
        {!loading && decks.length > 0 && visible.length === 0 && (
          <p className="library__empty">Nothing matches “{query}”.</p>
        )}

        <ul className="deckgrid">
          {visible.map((deck) => (
            <li key={deck.id} className={`card card--${deck.status}`}>
              {/* Every status goes to the same place: the deck page decides
                  whether that means the parsing view or the gallery. */}
              <a
                className="card__hit"
                href={`#/deck/${deck.id}`}
                aria-label={`Open ${deck.title}`}
              >
                <div className="card__thumb">
                  {deck.has_images && deck.slide_count > 0 ? (
                    <SlideImage deckId={deck.id} slideNumber={1} title={deck.title} />
                  ) : (
                    <span className="card__glyph" aria-hidden="true">
                      {deck.builtin ? "★" : "▤"}
                    </span>
                  )}
                  {deck.status === "processing" && (
                    <span className="card__badge card__badge--busy">Parsing…</span>
                  )}
                  {deck.status === "failed" && (
                    <span className="card__badge card__badge--bad">Failed</span>
                  )}
                </div>

                <div className="card__body">
                  <span className="card__title">
                    {deck.title}
                    {deck.builtin && <span className="deck__tag">built-in</span>}
                  </span>
                  <span className="card__meta">
                    {deck.status === "ready" && (
                      <>
                        {deck.slide_count} slide{deck.slide_count === 1 ? "" : "s"}
                        {!deck.builtin && ` · ${relativeTime(deck.created_at)}`}
                      </>
                    )}
                    {deck.status === "processing" && (
                      <>
                        {deck.page_count > 0
                          ? `${deck.progress || deck.pages_rendered} of ${deck.page_count}`
                          : "Starting…"}
                      </>
                    )}
                    {deck.status === "failed" && (
                      <span className="deck__error">
                        {deck.error ?? "Ingestion failed"}
                      </span>
                    )}
                  </span>
                  {deck.status === "processing" && deck.page_count > 0 && (
                    <span className="card__bar" aria-hidden="true">
                      <span
                        className="card__bar-fill"
                        style={{
                          width: `${Math.round(
                            ((deck.progress || deck.pages_rendered) /
                              deck.page_count) *
                              100,
                          )}%`,
                        }}
                      />
                    </span>
                  )}
                </div>
              </a>

              <div className="card__actions">
                {deck.status === "ready" && (
                  <a className="btn btn--primary btn--sm" href={`#/present/${deck.id}`}>
                    Present
                  </a>
                )}
                {!deck.builtin && (
                  <button
                    type="button"
                    className="btn btn--quiet btn--sm"
                    onClick={() => void handleDelete(deck)}
                    aria-label={`Delete ${deck.title}`}
                  >
                    Delete
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
