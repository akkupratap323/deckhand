"""Deck models, and the built-in deck the app ships with.

A ``Deck`` is either the built-in one below or the result of ingesting an
uploaded PDF (see ``ingest.py``). Either way the presenter works the same: the
frontend receives titles and bullets over the wire and renders what it is told,
while narration stays server-side and reaches the model only as a tool result.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

DEFAULT_DECK_ID = "clinical-trial-phases"

DeckStatus = Literal["processing", "ready", "failed"]
# Ingestion stages, in the order they run. "converting" only happens for
# PowerPoint uploads; PDFs start at "rendering".
IngestPhase = Literal[
    "queued", "converting", "rendering", "narrating", "synthesizing", "done"
]


@dataclass(frozen=True)
class Slide:
    """One slide.

    Attributes:
        id: Stable slide identifier, 1-indexed, matching its position in the deck.
        title: Slide heading, shown large in the UI.
        bullets: 3-4 short on-screen strings. Never read verbatim by the agent.
        narration: 2-4 sentences the agent speaks when presenting this slide.
        keywords: Terms that should route a question here. These go into the
            system prompt as routing hints, not into a matcher - see the README
            on why navigation is LLM-driven rather than keyword-based.
        transition: A short spoken bridge from the previous slide into this
            one, e.g. "Now that we've covered the problem, here's the fix."
            Written by the deck-level synthesis pass in ``narration.py``, which
            sees every slide's narration together and can write a transition
            that actually connects them - something no single slide's own
            narration call could do. Empty for slide 1, which has no
            predecessor, and for any deck synthesis has not run on yet.
    """

    id: int
    title: str
    bullets: list[str]
    narration: str
    keywords: list[str] = field(default_factory=list)
    transition: str = ""

    def to_public(self) -> dict[str, Any]:
        """Serialize for the frontend: everything except the narration script."""
        return {"id": self.id, "title": self.title, "bullets": list(self.bullets)}

    def to_tool_result(self) -> dict[str, Any]:
        """Serialize for a tool result - this is the grounding payload.

        The model answers from what this returns, which is why the narration is
        included here but not in the frontend payload.
        """
        return {
            "slide_number": self.id,
            "title": self.title,
            "bullets": list(self.bullets),
            "content": self.narration,
        }

    def to_storage(self) -> dict[str, Any]:
        """Full serialization, for persisting an ingested deck."""
        return {
            "id": self.id,
            "title": self.title,
            "bullets": list(self.bullets),
            "narration": self.narration,
            "keywords": list(self.keywords),
            "transition": self.transition,
        }

    @classmethod
    def from_storage(cls, raw: dict[str, Any]) -> Slide:
        return cls(
            id=int(raw["id"]),
            title=raw["title"],
            bullets=list(raw.get("bullets", [])),
            narration=raw.get("narration", ""),
            keywords=list(raw.get("keywords", [])),
            transition=raw.get("transition", ""),
        )


@dataclass
class Deck:
    """A presentable deck.

    Attributes:
        id: URL-safe identifier; also the share link.
        title: Deck title, used in the system prompt and the page header.
        slides: Ordered slides. ``slides[n - 1].id == n`` always holds.
        status: "processing" while ingestion runs, then "ready" or "failed".
        error: Failure reason when status is "failed".
        source: Original filename, or None for the built-in deck.
        created_at: ISO-8601 UTC timestamp.
        progress: Slides narrated so far, for the upload progress bar.
        has_images: Whether rendered page images exist for this deck. False for
            the built-in deck, which has no source PDF to render.
        phase: Which stage of ingestion is running, for the live parsing view.
            Rendering finishes long before narration does, and the two have
            very different durations, so a single percentage across both would
            move at a misleading rate. The UI shows them as distinct steps.
        page_count: Pages the source turned out to have. Known as soon as the
            document is opened, which is well before any narration exists -
            this is what lets the parsing view lay out its slots up front
            instead of growing the list one item at a time.
        pages_rendered: Page images written to disk so far. Each one is
            immediately fetchable, so the parsing view can show a real
            thumbnail the moment the count goes up.
        intro: The presenter's opening words, spoken before slide 1. Written by
            the synthesis pass from every slide's finished narration, so it can
            preview the actual talk rather than being generic. Empty until
            synthesis has run.
        outro: The presenter's closing words after the last slide.
        persona: A one- or two-sentence direction for who plausibly gives this
            talk and in what tone, inferred from the deck's subject matter.
            Never spoken aloud - folded into the system prompt as guidance.
    """

    id: str
    title: str
    slides: list[Slide] = field(default_factory=list)
    status: DeckStatus = "ready"
    error: str | None = None
    source: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    progress: int = 0
    has_images: bool = False
    phase: IngestPhase = "done"
    page_count: int = 0
    pages_rendered: int = 0
    intro: str = ""
    outro: str = ""
    persona: str = ""

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @property
    def builtin(self) -> bool:
        return self.id == DEFAULT_DECK_ID

    def get_slide(self, slide_number: int) -> Slide | None:
        """Return the slide with this 1-indexed number, or None if out of range."""
        if 1 <= slide_number <= len(self.slides):
            return self.slides[slide_number - 1]
        return None

    def public_slides(self) -> list[dict[str, Any]]:
        """The deck as the frontend receives it - titles and bullets only."""
        return [s.to_public() for s in self.slides]

    def apply_synthesis(self, synthesis: dict[str, Any]) -> None:
        """Fold a ``narration.synthesize_deck()`` result into this deck.

        Rebuilds ``slides`` because ``Slide`` is frozen - a transition is a new
        slide with one field changed, not a mutation of the old one.
        """
        self.intro = synthesis.get("intro", "")
        self.outro = synthesis.get("outro", "")
        self.persona = synthesis.get("persona", "")
        transitions: dict[int, str] = synthesis.get("transitions", {})
        self.slides = [
            dataclasses.replace(s, transition=transitions.get(s.id, s.transition))
            for s in self.slides
        ]

    def summary(self) -> dict[str, Any]:
        """Library-listing shape: everything except slide content."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "error": self.error,
            "source": self.source,
            "created_at": self.created_at,
            "slide_count": self.slide_count,
            "progress": self.progress,
            "phase": self.phase,
            "page_count": self.page_count,
            "pages_rendered": self.pages_rendered,
            "builtin": self.builtin,
            "has_images": self.has_images,
            "has_synthesis": bool(self.intro),
        }

    def to_storage(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "slides": [s.to_storage() for s in self.slides],
            "intro": self.intro,
            "outro": self.outro,
            "persona": self.persona,
        }

    @classmethod
    def from_storage(cls, raw: dict[str, Any]) -> Deck:
        return cls(
            id=raw["id"],
            title=raw["title"],
            slides=[Slide.from_storage(s) for s in raw.get("slides", [])],
            status=raw.get("status", "ready"),
            error=raw.get("error"),
            source=raw.get("source"),
            created_at=raw.get("created_at", datetime.now(UTC).isoformat()),
            progress=raw.get("progress", 0),
            phase=raw.get("phase", "done"),
            page_count=raw.get("page_count", 0),
            pages_rendered=raw.get("pages_rendered", 0),
            has_images=raw.get("has_images", False),
            intro=raw.get("intro", ""),
            outro=raw.get("outro", ""),
            persona=raw.get("persona", ""),
        )


def new_deck_id() -> str:
    """Short, URL-safe, unguessable enough for a share link."""
    return uuid.uuid4().hex[:12]


# --- the built-in deck ------------------------------------------------------
# Factual and textbook-level. It describes how the clinical trial process works;
# it deliberately contains no drug names, no efficacy claims, and nothing that
# could read as medical advice. See prompts.py for the refusal policy that keeps
# the agent inside this boundary at runtime.

BUILTIN_SLIDES: list[Slide] = [
    Slide(
        id=1,
        title="What a Clinical Trial Is",
        bullets=[
            "A structured study testing a treatment in people",
            "Phases exist to answer one question at a time",
            "Regulators require evidence before approval",
            "Safety gates every step forward",
        ],
        narration=(
            "A clinical trial is a structured research study that tests a potential treatment "
            "in people, under rules set by regulators such as the FDA in the United States or "
            "the EMA in Europe. The work is split into phases because each phase is designed to "
            "answer one question at a time: is it safe, does it work, does it hold up at scale. "
            "Nothing moves to the next phase until the current one clears its safety bar, which "
            "is why the process is deliberately sequential rather than fast."
        ),
        keywords=[
            "clinical trial", "what is a trial", "definition", "regulator", "FDA", "EMA",
            "why phases", "overview", "purpose", "approval process", "oversight",
        ],
    ),
    Slide(
        id=2,
        title="Preclinical and IND",
        bullets=[
            "Laboratory and animal studies come first",
            "Screens for toxicity before any human dose",
            "IND application filed with the regulator",
            "Approval of the IND unlocks Phase I",
        ],
        narration=(
            "Before a treatment reaches a single person, it goes through preclinical work: "
            "laboratory studies and animal studies that screen for toxicity and give a first "
            "estimate of a safe starting dose. If that data looks acceptable, the sponsor files "
            "an Investigational New Drug application, an IND, with the regulator. The IND bundles "
            "the preclinical results, the manufacturing details, and the proposed trial protocol. "
            "Only once the regulator allows the IND to proceed can human testing begin."
        ),
        keywords=[
            "preclinical", "animal studies", "lab studies", "toxicity", "IND",
            "investigational new drug", "before humans", "first step", "protocol", "sponsor",
        ],
    ),
    Slide(
        id=3,
        title="Phase I - Safety and Dosage",
        bullets=[
            "Roughly 20 to 100 participants",
            "Usually healthy volunteers",
            "Question: is it safe, and at what dose",
            "Takes several months to about a year",
        ],
        narration=(
            "Phase One is the first time the treatment is given to people, typically twenty to a "
            "hundred participants, and usually healthy volunteers rather than patients. The "
            "question here is not whether it works, but whether it is safe, how the body "
            "processes it, and what dose range can be tolerated. Doses start very low and "
            "escalate slowly while researchers watch for side effects. This phase generally runs "
            "from several months to about a year."
        ),
        keywords=[
            "phase one", "phase 1", "safety", "dosage", "dose", "healthy volunteers",
            "first in human", "twenty to a hundred", "tolerability", "smallest",
        ],
    ),
    Slide(
        id=4,
        title="Phase II - Efficacy and Side Effects",
        bullets=[
            "Roughly 100 to 300 participants",
            "Participants actually have the condition",
            "Question: does it work, and how well",
            "Commonly runs one to two years",
        ],
        narration=(
            "Phase Two is where the question shifts from safety to effectiveness. The treatment "
            "now goes to roughly one hundred to three hundred participants who actually have the "
            "condition being studied, rather than healthy volunteers. Researchers are looking for "
            "a signal that it works, while continuing to collect safety and side effect data at a "
            "larger scale. This phase commonly takes one to two years, and it is where a large "
            "share of candidates fail."
        ),
        keywords=[
            "phase two", "phase 2", "efficacy", "does it work", "side effects",
            "hundred to three hundred", "patients with the condition", "signal", "effectiveness",
        ],
    ),
    Slide(
        id=5,
        title="Phase III - Large-Scale Confirmation",
        bullets=[
            "Roughly 1,000 to 3,000 participants",
            "Compared against existing standard of care",
            "Question: does the result hold at scale",
            "Typically one to four years",
        ],
        narration=(
            "Phase Three is the large confirmatory study, usually one thousand to three thousand "
            "participants across many sites. The treatment is compared against the existing "
            "standard of care or a placebo, and the size is what makes the result statistically "
            "convincing and surfaces less common side effects that smaller phases would miss. "
            "This is the longest and most expensive stage, typically one to four years, and its "
            "data forms the core of the submission to regulators."
        ),
        keywords=[
            "phase three", "phase 3", "large scale", "thousand", "one thousand",
            "three thousand", "confirmation", "standard of care", "placebo", "comparison",
            "longest", "most expensive", "biggest", "largest",
        ],
    ),
    Slide(
        id=6,
        title="Phase IV and Post-Market",
        bullets=[
            "Begins after regulatory approval",
            "Monitors the treatment in real-world use",
            "Catches rare, long-term effects",
            "Can trigger label changes or withdrawal",
        ],
        narration=(
            "Phase Four starts after the treatment is approved and available. Once it is used by "
            "far more people, and for longer, than any trial could cover, ongoing surveillance "
            "looks for rare or long-term effects that earlier phases were too small or too short "
            "to detect. What turns up here can lead to updated labelling, new warnings, "
            "restrictions on use, or in serious cases withdrawal from the market. It is the part "
            "of the process that never really ends."
        ),
        keywords=[
            "phase four", "phase 4", "post market", "after approval", "surveillance",
            "monitoring", "rare effects", "long term", "withdrawal", "label",
            "pharmacovigilance", "last", "final",
        ],
    ),
]


def builtin_deck() -> Deck:
    """A fresh copy of the deck the app ships with."""
    return Deck(
        id=DEFAULT_DECK_ID,
        title="Understanding Clinical Trial Phases",
        slides=list(BUILTIN_SLIDES),
        status="ready",
        source=None,
    )
