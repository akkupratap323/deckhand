/**
 * Choose who presents the deck, before the session starts.
 *
 * Each presenter is a real Cartesia voice paired with delivery direction that
 * goes into the system prompt - so the choice changes both how the agent
 * sounds and how it talks: what it leads with, how much it explains, whether
 * it opens a slide with a conclusion or a question.
 *
 * Deliberately a step of its own rather than a dropdown next to the start
 * button. It cannot be changed mid-session (the voice is fixed when the
 * pipeline is built), so it belongs before the session as a decision, not
 * beside it as a setting someone would reasonably expect to toggle live.
 *
 * The card carries a soft gradient orb rather than a portrait. These are
 * synthetic voices, and a stock human face would imply a specific person
 * behind one - the orb reads as "a voice" without making that claim, and
 * gives each presenter something instantly distinguishable at a glance.
 */

import type { Presenter } from "../types";

interface Props {
  presenters: Presenter[];
  selected: string | null;
  onSelect: (id: string) => void;
  onStart: () => void;
  /** Disables the start button while connecting. */
  busy?: boolean;
  deckTitle: string;
}

/**
 * Card glyphs, keyed by the icon name the server sends.
 *
 * Inline paths rather than an icon package: six 24px glyphs do not justify a
 * dependency, and keeping them here means the server never ships markup.
 */
const ICONS: Record<string, string> = {
  chart: "M3 3v18h18M7 15l3-4 3 3 5-7",
  spark: "M12 3l2.1 5.4L20 10l-5.9 1.6L12 17l-2.1-5.4L4 10l5.9-1.6z",
  book: "M4 5a2 2 0 012-2h12v18H6a2 2 0 01-2-2zM8 3v18",
  brief: "M3 8h18v11a2 2 0 01-2 2H5a2 2 0 01-2-2zM9 8V5a2 2 0 012-2h2a2 2 0 012 2v3",
  quote: "M8 7H5a2 2 0 00-2 2v3h5zm11 0h-3a2 2 0 00-2 2v3h5zM3 12c0 4 2 5 5 5m6-5c0 4 2 5 5 5",
  compass: "M12 21a9 9 0 100-18 9 9 0 000 18zM15.5 8.5l-2 5-5 2 2-5z",
};

function Glyph({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={ICONS[name] ?? ICONS.spark} />
    </svg>
  );
}

export function PresenterPicker({
  presenters,
  selected,
  onSelect,
  onStart,
  busy,
  deckTitle,
}: Props) {
  return (
    <section className="picker">
      <header className="picker__head">
        <p className="picker__eyebrow">Voice presenter</p>
        <h2 className="picker__title">Choose your presenter</h2>
        <p className="picker__sub">
          Each one has a different voice and a different way of working through a
          deck. They cover the same material — <strong>{deckTitle}</strong> — with the
          same grounding rules.
        </p>
      </header>

      <div className="picker__grid" role="radiogroup" aria-label="Presenter">
        {presenters.map((p) => {
          const isOn = p.id === selected;
          return (
            <article key={p.id} className="pcard2" data-selected={isOn}>
              {/* The orb panel. Colours come from the server so the card and
                  the voice stay associated in one place. */}
              <div
                className="pcard2__stage"
                style={
                  {
                    "--orb-from": p.colors[0],
                    "--orb-to": p.colors[1],
                  } as React.CSSProperties
                }
              >
                <span className="pcard2__orb" aria-hidden="true" />
                <span className="pcard2__pill">{p.category}</span>
              </div>

              <div className="pcard2__body">
                <div className="pcard2__head">
                  <span className="pcard2__icon" aria-hidden="true">
                    <Glyph name={p.icon} />
                  </span>
                  <span className="pcard2__id">
                    <span className="pcard2__name">
                      {p.name} — {p.tagline}
                    </span>
                    <span className="pcard2__meta">
                      {p.accent} · {p.tone.join(" · ")}
                    </span>
                  </span>
                </div>

                <p className="pcard2__blurb">{p.blurb}</p>

                <button
                  type="button"
                  role="radio"
                  aria-checked={isOn}
                  className="pcard2__btn"
                  onClick={() => onSelect(p.id)}
                >
                  {isOn ? "Selected" : "Choose"}
                </button>
              </div>
            </article>
          );
        })}
      </div>

      <div className="picker__actions">
        <button
          type="button"
          className="btn btn--primary btn--lg"
          onClick={onStart}
          disabled={!selected || busy}
        >
          {busy ? "Connecting…" : "Start presentation"}
        </button>
        <span className="picker__hint">
          You can interrupt and ask questions at any point once it begins.
        </span>
      </div>
    </section>
  );
}
