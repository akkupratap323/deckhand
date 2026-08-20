/**
 * What you watch while a deck is being ingested.
 *
 * The backend publishes four things worth showing - the phase, the page count,
 * how many pages have rendered, and how many slides have been narrated - and
 * this turns them into something legible: a stepper for where we are, a bar
 * for the work inside the current step, and a grid of real thumbnails that
 * fill in as each page lands.
 *
 * Deliberately not one combined percentage. Rendering is fast and local while
 * narration is slow and network-bound, so a single bar across both would crawl
 * and then leap. Two honest steps beat one dishonest number.
 */

import type { DeckStatus, IngestPhase } from "../types";
import { SlideImage } from "./SlideImage";

interface Props {
  deckId: string;
  status: DeckStatus;
  /** Shown while the very first poll is still in flight. */
  sourceName?: string | null;
}

/** The steps a user sees. "queued" and "synthesizing" fold into neighbours. */
const STEPS: { key: IngestPhase; label: string; hint: string }[] = [
  { key: "converting", label: "Converting", hint: "PowerPoint to PDF" },
  { key: "rendering", label: "Rendering", hint: "drawing each page" },
  { key: "narrating", label: "Writing narration", hint: "reading every slide" },
  { key: "synthesizing", label: "Finishing", hint: "intro, transitions, outro" },
];

/** Where each phase sits in the visible sequence. */
const ORDER: Record<IngestPhase, number> = {
  queued: 0,
  converting: 0,
  rendering: 1,
  narrating: 2,
  synthesizing: 3,
  done: 4,
};

function stepState(step: IngestPhase, current: IngestPhase): "done" | "active" | "todo" {
  const at = ORDER[current];
  const mine = ORDER[step];
  if (at > mine) return "done";
  if (at === mine) return "active";
  return "todo";
}

export function Parsing({ deckId, status, sourceName }: Props) {
  const { phase, page_count: pages, pages_rendered: rendered, progress } = status;

  // Only the step in flight gets a bar, and it measures that step's own work.
  const narrating = phase === "narrating" || phase === "synthesizing";
  const unit = narrating ? progress : rendered;
  const total = pages || 0;
  const pct = total > 0 ? Math.min(100, Math.round((unit / total) * 100)) : 0;

  // A PowerPoint upload starts at "converting"; a PDF never visits it, so
  // hide the step entirely rather than showing one that stays grey forever.
  const steps = STEPS.filter(
    (s) => s.key !== "converting" || ORDER[phase] === 0,
  );

  const caption = (() => {
    switch (phase) {
      case "queued":
        return "Getting started…";
      case "converting":
        return "Converting your PowerPoint with LibreOffice…";
      case "rendering":
        return total
          ? `Rendering page ${Math.min(rendered + 1, total)} of ${total}…`
          : "Opening the document…";
      case "narrating":
        return `Writing narration — ${progress} of ${total} slides done`;
      case "synthesizing":
        return "Writing the intro, transitions and closing…";
      default:
        return "Almost there…";
    }
  })();

  return (
    <section className="parsing" aria-live="polite" aria-busy="true">
      <header className="parsing__head">
        <h2 className="parsing__title">{status.title || sourceName || "Your deck"}</h2>
        <p className="parsing__caption">{caption}</p>
      </header>

      <ol className="stepper">
        {steps.map((step) => {
          const state = stepState(step.key, phase);
          return (
            <li key={step.key} className="stepper__item" data-state={state}>
              <span className="stepper__dot" aria-hidden="true">
                {state === "done" ? "✓" : ""}
              </span>
              <span className="stepper__text">
                <span className="stepper__label">{step.label}</span>
                <span className="stepper__hint">{step.hint}</span>
              </span>
            </li>
          );
        })}
      </ol>

      <div className="parsing__bar">
        <div
          className="parsing__fill"
          style={{ width: `${pct}%` }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={narrating ? "Narration progress" : "Rendering progress"}
        />
      </div>

      {total > 0 && (
        <div className="parsing__grid">
          {Array.from({ length: total }, (_, i) => i + 1).map((n) => {
            // A page is only requestable once the backend says it rendered.
            const ready = n <= rendered;
            const narrated = narrating && n <= progress;
            return (
              <figure
                key={n}
                className="pcard"
                data-ready={ready}
                data-narrated={narrated}
              >
                {ready ? (
                  <SlideImage deckId={deckId} slideNumber={n} loading="lazy" />
                ) : (
                  <div className="pcard__pending" aria-hidden="true" />
                )}
                <figcaption className="pcard__cap">
                  <span>{n}</span>
                  {narrated && (
                    <span className="pcard__tick" title="Narration written">
                      ✓
                    </span>
                  )}
                </figcaption>
              </figure>
            );
          })}
        </div>
      )}
    </section>
  );
}
