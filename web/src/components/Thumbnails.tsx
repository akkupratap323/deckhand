/**
 * Slide strip showing where the talk is in the deck.
 *
 * Read-only on purpose. Clicking a thumbnail to change slides would put slide
 * state in two places — the backend owns it, and the agent has to know what is
 * on screen to answer from it.
 */

import type { Slide } from "../types";

interface Props {
  slides: Slide[];
  current: number;
}

export function Thumbnails({ slides, current }: Props) {
  if (slides.length === 0) return null;

  return (
    <ol className="strip" aria-label="Deck overview">
      {slides.map((slide) => (
        <li
          key={slide.id}
          className={`strip__item${slide.id === current ? " strip__item--on" : ""}`}
          aria-current={slide.id === current ? "step" : undefined}
        >
          <span className="strip__num">{slide.id}</span>
          <span className="strip__title">{slide.title}</span>
        </li>
      ))}
    </ol>
  );
}
