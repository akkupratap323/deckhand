/**
 * Review a finished deck before presenting it.
 *
 * One large page image, a filmstrip of every slide, and what the agent
 * extracted for the selected one. The point is to let someone confirm the deck
 * parsed correctly - right pages, sensible titles, narration that matches the
 * slide - before handing it to the voice agent, rather than discovering a bad
 * parse halfway through a live presentation.
 *
 * Keyboard navigation is first-class here: arrow keys and Home/End move
 * between slides, which is how anyone actually reviews a deck of any length.
 */

import { useCallback, useEffect, useRef } from "react";

import type { DeckDetail } from "../types";
import { SlideImage } from "./SlideImage";

interface Props {
  deck: DeckDetail;
  selected: number;
  onSelect: (slideNumber: number) => void;
  onStart: () => void;
  /** True while the synthesis pass is still running; it only adds polish. */
  finishing?: boolean;
}

export function Gallery({ deck, selected, onSelect, onStart, finishing }: Props) {
  const stripRef = useRef<HTMLDivElement>(null);
  const total = deck.slides.length;
  const slide = deck.slides.find((s) => s.id === selected) ?? deck.slides[0];

  const move = useCallback(
    (to: number) => {
      if (total === 0) return;
      onSelect(Math.min(total, Math.max(1, to)));
    },
    [onSelect, total],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Never hijack keys while someone is typing somewhere.
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;

      switch (event.key) {
        case "ArrowRight":
        case "ArrowDown":
          event.preventDefault();
          move(selected + 1);
          break;
        case "ArrowLeft":
        case "ArrowUp":
          event.preventDefault();
          move(selected - 1);
          break;
        case "Home":
          event.preventDefault();
          move(1);
          break;
        case "End":
          event.preventDefault();
          move(total);
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [move, selected, total]);

  // Keep the active thumbnail in view when moving by keyboard.
  //
  // Scrolls the filmstrip itself rather than calling scrollIntoView on the
  // thumbnail: that method scrolls every scrollable ancestor, so centring a
  // thumbnail would also scroll the page under the reader.
  useEffect(() => {
    const strip = stripRef.current;
    const active = strip?.querySelector<HTMLElement>('[data-active="true"]');
    if (!strip || !active) return;
    strip.scrollTo({
      left: active.offsetLeft - (strip.clientWidth - active.clientWidth) / 2,
      behavior: "smooth",
    });
  }, [selected]);

  if (!slide) {
    return <p className="empty">This deck has no slides.</p>;
  }

  return (
    <section className="gallery">
      <div className="gallery__stage">
        <button
          type="button"
          className="gallery__arrow gallery__arrow--prev"
          onClick={() => move(selected - 1)}
          disabled={selected <= 1}
          aria-label="Previous slide"
        >
          ‹
        </button>

        {deck.has_images ? (
          <SlideImage
            deckId={deck.id}
            slideNumber={slide.id}
            title={slide.title}
            loading="eager"
            className="slideimg--stage"
          />
        ) : (
          // The built-in deck has no source file to render, so show the
          // structured content instead of an empty frame.
          <div className="gallery__textslide">
            <h3>{slide.title}</h3>
            <ul>
              {slide.bullets.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          </div>
        )}

        <button
          type="button"
          className="gallery__arrow gallery__arrow--next"
          onClick={() => move(selected + 1)}
          disabled={selected >= total}
          aria-label="Next slide"
        >
          ›
        </button>
      </div>

      <div className="gallery__meta">
        <div className="gallery__metatop">
          <span className="chip">
            Slide {slide.id} of {total}
          </span>
          <h3 className="gallery__slidetitle">{slide.title}</h3>
        </div>
        {slide.bullets.length > 0 && (
          <ul className="gallery__bullets">
            {slide.bullets.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        )}
      </div>

      <div className="filmstrip" ref={stripRef} role="tablist" aria-label="Slides">
        {deck.slides.map((s) => (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={s.id === selected}
            data-active={s.id === selected}
            className="filmstrip__item"
            onClick={() => onSelect(s.id)}
            title={s.title}
          >
            {deck.has_images ? (
              <SlideImage deckId={deck.id} slideNumber={s.id} title={s.title} />
            ) : (
              <span className="filmstrip__text">{s.title}</span>
            )}
            <span className="filmstrip__num">{s.id}</span>
          </button>
        ))}
      </div>

      <div className="gallery__actions">
        <button type="button" className="btn btn--primary btn--lg" onClick={onStart}>
          Start presentation
        </button>
        {finishing && (
          <span className="gallery__finishing">
            Still writing transitions — you can start now, or wait a moment for the
            full script.
          </span>
        )}
      </div>
    </section>
  );
}
