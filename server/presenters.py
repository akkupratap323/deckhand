"""The presenters you can choose between before a talk starts.

Each one is a voice plus a manner of speaking. They present the same deck with
the same facts and the same grounding rules - what changes is delivery: pace,
warmth, how much they explain versus assert, and whether they open with a
question or a statement.

The voice ids are real Cartesia library voices, taken from that account's
``/voices`` listing rather than invented. ``scripts/check_voices.py`` verifies
they still resolve, because a voice id that has been retired upstream fails at
session start - the least convenient moment to discover it.

Deck persona vs presenter persona
---------------------------------
These are different things and both are used. The deck's own ``persona`` field
is written by the ingestion pass and describes *who would plausibly give this
particular talk* - a clinician for a trials deck, an engineer for an
architecture one. The presenter chosen here describes *how this speaker
delivers anything*. The deck persona sets the domain register; the presenter
sets the temperament. ``prompts.build_system_prompt`` folds the presenter in
underneath the deck persona so subject-matter framing still wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Presenter:
    """One selectable presenter.

    Attributes:
        id: Stable slug, used in URLs and sent back on the signalling request.
        name: Display name.
        tagline: One short line for the selection card.
        blurb: A sentence on when this presenter suits a deck.
        voice_id: Cartesia library voice.
        style: Folded into the system prompt. Written as direction to a
            speaker, not as a description of one - the model follows an
            instruction far more reliably than it imitates an adjective.
        accent: Shown on the card so the choice is not blind.
        tone: Two or three words for the card's tag row.
        category: One-word grouping, shown as a pill on the card.
        icon: Which glyph the card draws. The frontend maps this to an inline
            SVG; kept as a name here so the server stays free of markup.
        colors: Two hex stops for the card's orb gradient. Given per presenter
            rather than generated from the id, because a hash-derived hue
            lands on muddy colours often enough to look like a bug.
    """

    id: str
    name: str
    tagline: str
    blurb: str
    voice_id: str
    style: str
    accent: str
    tone: tuple[str, ...]
    category: str
    icon: str
    colors: tuple[str, str]

    def public(self) -> dict[str, Any]:
        """Everything the picker needs. The voice id is included deliberately:
        it is not a secret, and the client echoes it back on the offer."""
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "blurb": self.blurb,
            "accent": self.accent,
            "tone": list(self.tone),
            "category": self.category,
            "icon": self.icon,
            "colors": list(self.colors),
        }


PRESENTERS: tuple[Presenter, ...] = (
    Presenter(
        id="analyst",
        name="Clive",
        tagline="The Analyst",
        blurb="Precise and unhurried. Best for technical or data-heavy decks.",
        voice_id="b24f41fd-00a3-4cd8-992a-a0c9f13f3ef1",
        style=(
            "Speak precisely and without hurry. Lead with the number or the fact, then "
            "say what it means. Prefer one exact sentence to three approximate ones. Do "
            "not use filler openers like 'so' or 'basically'. When something is "
            "uncertain, say so plainly rather than smoothing it over."
        ),
        accent="British",
        tone=("Measured", "Precise", "Formal"),
        category="Technical",
        icon="chart",
        colors=("#5b7cfa", "#2a3fd4"),
    ),
    Presenter(
        id="host",
        name="Finn",
        tagline="The Host",
        blurb="Warm and energetic. Good for demos and introductory material.",
        voice_id="15070120-82ab-48e5-87e5-c4bf28fa4bf9",
        style=(
            "Speak with energy and warmth, like someone genuinely pleased to be showing "
            "this. Use short sentences and everyday words. It is fine to react to what "
            "is on the slide - 'this next one is the interesting bit'. Keep the pace up "
            "and never labour a point."
        ),
        accent="British",
        tone=("Upbeat", "Warm", "Brisk"),
        category="Demos",
        icon="spark",
        colors=("#ffb347", "#f4761f"),
    ),
    Presenter(
        id="educator",
        name="Ellen",
        tagline="The Educator",
        blurb="Patient and clear. Explains the why before the what.",
        voice_id="a151affa-feaa-439e-8df8-c1d3f91dc6b9",
        style=(
            "Teach rather than announce. Give the reason before the detail, and check "
            "understanding as you go - 'the thing to hold on to here is...'. Define a "
            "term the first time it appears. Never assume the audience already knows "
            "why something matters."
        ),
        accent="American",
        tone=("Patient", "Clear", "Warm"),
        category="Teaching",
        icon="book",
        colors=("#c77dff", "#8b3fd9"),
    ),
    Presenter(
        id="briefer",
        name="Harvey",
        tagline="The Briefer",
        blurb="Calm and economical. Executive-briefing pace, no padding.",
        voice_id="1df0b2fe-acb0-4484-8f32-9e231d9e7a90",
        style=(
            "Deliver an executive briefing. Lead every slide with its conclusion, then "
            "support it in one or two sentences. Cut anything that does not change a "
            "decision. Stay calm and level even when the material is dramatic. Be "
            "comfortable finishing a slide early."
        ),
        accent="British",
        tone=("Calm", "Concise", "Direct"),
        category="Executive",
        icon="brief",
        colors=("#4fd1c5", "#12908a"),
    ),
    Presenter(
        id="narrator",
        name="Tamsin",
        tagline="The Narrator",
        blurb="Story-led. Carries a thread from one slide to the next.",
        voice_id="24894159-1d4e-4b7c-80ca-4ae37dce9400",
        style=(
            "Tell the deck as a continuous story rather than a list. Make each slide "
            "follow from the last - what changed, what it led to. Use the transitions "
            "you are given and build on them. Favour narrative connectives over "
            "enumeration: 'which is why', 'that left them with', rather than 'secondly'."
        ),
        accent="British",
        tone=("Narrative", "Flowing", "Engaging"),
        category="Storytelling",
        icon="quote",
        colors=("#ff8fab", "#e0416f"),
    ),
    Presenter(
        id="guide",
        name="Bronte",
        tagline="The Guide",
        blurb="Approachable and plain-spoken. Good for mixed audiences.",
        voice_id="391f4c0a-f1a8-4c21-9aa2-7a07f0a4b0dc",
        style=(
            "Be approachable and plain-spoken. Translate jargon into ordinary language "
            "the moment it appears, and say when something is more complicated than you "
            "are making it sound. Address the audience directly as 'you'. Sound like a "
            "knowledgeable colleague, not a lecturer."
        ),
        accent="Australian",
        tone=("Friendly", "Plain", "Direct"),
        category="General",
        icon="compass",
        colors=("#9ae66e", "#4c9f2f"),
    ),
)


BY_ID: dict[str, Presenter] = {p.id: p for p in PRESENTERS}

# Used when nothing is chosen, and by the smoke test, which has no UI to pick
# from. Overridable so a deployment can lead with a different default.
DEFAULT_PRESENTER_ID = os.environ.get("DEFAULT_PRESENTER", "educator")


def get(presenter_id: str | None) -> Presenter:
    """Resolve a presenter id, falling back rather than failing.

    An unknown id means a stale link or a client sending something the server
    no longer offers. Refusing the session over that would be worse than
    presenting in the default voice, so this never raises.
    """
    if presenter_id:
        found = BY_ID.get(presenter_id.strip().lower())
        if found:
            return found
    return BY_ID.get(DEFAULT_PRESENTER_ID) or PRESENTERS[0]


def voice_id_for(presenter: Presenter) -> str:
    """The Cartesia voice to speak with.

    The presenter's own voice always wins. ``CARTESIA_VOICE_ID`` is only a
    fallback for a presenter with no voice of its own, which no shipped one
    has - deliberately, because letting that variable override the choice
    would make every presenter sound identical and quietly turn the picker
    into decoration. Set ``CARTESIA_VOICE_ID`` to pin a single cloned voice
    only if that is genuinely what you want.
    """
    return presenter.voice_id or os.environ.get("CARTESIA_VOICE_ID", "").strip()
