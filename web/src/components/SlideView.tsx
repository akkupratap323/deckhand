/**
 * The slide currently on screen.
 *
 * When the deck came from a PDF, the stage is the *rendered page* — the real
 * slide, charts and layout included — not a reconstruction of it from extracted
 * text. The generated talking points sit beneath it, and the one the agent is
 * currently speaking to is highlighted.
 *
 * The built-in deck has no source PDF, so it falls back to rendering the
 * bullets large, which is the whole slide in that case.
 *
 * Bullets are real `<li>` elements carrying stable ids and `aria-label`s. That
 * is partly accessibility and partly forward planning: an accessibility
 * snapshot is exactly what a `UIWorker` would need to address these elements by
 * name. See the README on why the highlight ships over a plain server message
 * instead.
 */

import { useEffect, useState } from "react";

import { SERVER_URL } from "../api";
import type { Slide } from "../types";

interface Props {
  slide: Slide | null;
  highlighted: number | null;
  connected: boolean;
  /** Deck being presented, for building the image URL. */
  deckId: string;
  /** Whether this deck has rendered page images at all. */
  hasImages: boolean;
}

export function SlideView({
  slide,
  highlighted,
  connected,
  deckId,
  hasImages,
}: Props) {
  // Reset per slide so a previous failure does not hide the next image.
  const [imageFailed, setImageFailed] = useState(false);
  useEffect(() => setImageFailed(false), [slide?.id]);

  if (!slide) {
    return (
      <section className="slide slide--empty" aria-live="polite">
        <p className="slide__placeholder">
          {connected ? "Loading the deck…" : "Press Start presentation to begin."}
        </p>
      </section>
    );
  }

  const showImage = hasImages && !imageFailed;

  return (
    <section className="slide" aria-labelledby={`slide-${slide.id}-title`}>
      {showImage ? (
        <figure className="slide__figure" key={slide.id}>
          <img
            className="slide__image"
            src={`${SERVER_URL}/api/decks/${encodeURIComponent(deckId)}/slides/${slide.id}/image`}
            alt={`Slide ${slide.id}: ${slide.title}`}
            onError={() => setImageFailed(true)}
          />
          <figcaption id={`slide-${slide.id}-title`} className="slide__caption">
            {slide.title}
          </figcaption>
        </figure>
      ) : (
        <h2 id={`slide-${slide.id}-title`} className="slide__title" key={slide.id}>
          {slide.title}
        </h2>
      )}

      <ul className={`slide__bullets${showImage ? " slide__bullets--compact" : ""}`}>
        {slide.bullets.map((bullet, index) => {
          const isOn = highlighted === index;
          return (
            <li
              key={bullet}
              id={`slide-${slide.id}-bullet-${index}`}
              aria-label={bullet}
              aria-current={isOn ? "true" : undefined}
              className={`bullet${isOn ? " bullet--on" : ""}`}
              style={{ animationDelay: `${index * 70}ms` }}
            >
              <span className="bullet__marker" aria-hidden="true" />
              <span className="bullet__text">{bullet}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
