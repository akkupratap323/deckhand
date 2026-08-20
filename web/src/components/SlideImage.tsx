/**
 * A rendered page image, with the states it actually passes through.
 *
 * During ingestion these images appear one at a time, so a given slide's URL
 * is a 404 until its page finishes rendering. The parsing view only asks for
 * images it has been told exist, but the gap between "the count went up" and
 * "the file is served" is real, and a broken-image icon is a poor way to show
 * it. So: skeleton while loading, a quiet retry on error, and a text fallback
 * for decks that have no images at all (the built-in one).
 */

import { useEffect, useRef, useState } from "react";

import { slideImageUrl } from "../api";

interface Props {
  deckId: string;
  slideNumber: number;
  /** Alt text. Falls back to the slide number when the title is unknown. */
  title?: string;
  /** Eager for the large preview, lazy for thumbnails far down a grid. */
  loading?: "eager" | "lazy";
  className?: string;
}

/** How many times to retry a failed image before showing the fallback. */
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 900;

export function SlideImage({
  deckId,
  slideNumber,
  title,
  loading = "lazy",
  className,
}: Props) {
  const [state, setState] = useState<"loading" | "loaded" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const timer = useRef<number | undefined>(undefined);

  // A new slide is a new image; reset rather than showing the previous one's
  // resolved state while the new file loads. Adjusted during render rather
  // than in an effect - React's documented way to reset state on a prop
  // change, and it avoids rendering one frame of stale state first.
  const identity = `${deckId}:${slideNumber}`;
  const [seen, setSeen] = useState(identity);
  if (seen !== identity) {
    setSeen(identity);
    setState("loading");
    setAttempt(0);
  }

  useEffect(() => {
    return () => window.clearTimeout(timer.current);
  }, []);

  const handleError = () => {
    if (attempt >= MAX_RETRIES) {
      setState("failed");
      return;
    }
    // Back off a little and remount the <img> by bumping `attempt`, which is
    // part of the key. Changing the URL instead would defeat the cache for
    // every subsequent load of a perfectly good image.
    timer.current = window.setTimeout(
      () => setAttempt((n) => n + 1),
      RETRY_DELAY_MS * (attempt + 1),
    );
  };

  if (state === "failed") {
    return (
      <div className={`slideimg slideimg--failed ${className ?? ""}`}>
        <span className="slideimg__num">{slideNumber}</span>
      </div>
    );
  }

  return (
    <div className={`slideimg ${className ?? ""}`} data-state={state}>
      {state === "loading" && <div className="slideimg__skeleton" aria-hidden="true" />}
      <img
        key={attempt}
        src={slideImageUrl(deckId, slideNumber)}
        alt={title ? `Slide ${slideNumber}: ${title}` : `Slide ${slideNumber}`}
        loading={loading}
        decoding="async"
        draggable={false}
        onLoad={() => setState("loaded")}
        onError={handleError}
      />
    </div>
  );
}
