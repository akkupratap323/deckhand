"""Presentation state - the backend's authoritative view of the deck.

The frontend never decides which slide is current. It renders what this object
says, delivered over the RTVI server-message channel. Every mutation goes
through the helpers here so there is exactly one place that can change the
current slide, and exactly one place that records why.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from slides import Deck

Mode = Literal["presenting", "answering", "paused"]


@dataclass
class RefusalRecord:
    """One logged refusal - an off-deck question the agent declined to answer.

    Kept so the refusal behaviour can be demonstrated rather than asserted; the
    server prints a summary of these when the session ends.
    """

    slide: int
    topic: str


@dataclass
class PresentationState:
    """Mutable session state shared with tool handlers via ``app_resources``.

    Attributes:
        current_slide_index: 0-indexed position in the deck. Tool handlers read
            and write this; everything else treats it as read-only.
        mode: What the agent is doing. "presenting" while narrating, "answering"
            during a question detour, "paused" when the user asked it to stop.
        slides_narrated: Slide numbers (1-indexed) already presented, so the
            agent can tell a first visit from a revisit.
        highlighted_bullet: 0-indexed bullet currently highlighted on screen, or
            None. Cleared on every slide change.
        refusals: Off-deck questions the agent declined.
        turn_ended_at: ``time.monotonic()`` when Flux last confirmed the
            user's turn was over, or None before the first turn. The zero
            point for the latency logging in ``latency.py`` and ``tools.py`` -
            see those for what gets measured from it.
    """

    current_slide_index: int = 0
    mode: Mode = "presenting"
    slides_narrated: set[int] = field(default_factory=set)
    highlighted_bullet: int | None = None
    refusals: list[RefusalRecord] = field(default_factory=list)
    turn_ended_at: float | None = None

    @property
    def current_slide_number(self) -> int:
        """The current slide as a 1-indexed number, matching ``Slide.id``."""
        return self.current_slide_index + 1

    def set_slide(self, slide_number: int, slide_count: int) -> bool:
        """Move to a 1-indexed slide number.

        Clears any bullet highlight, since a highlight only ever refers to the
        slide that was showing when it was set.

        Args:
            slide_number: Target slide, 1-indexed.
            slide_count: Number of slides in the deck being presented.

        Returns:
            True if the slide changed, False if it was out of range or already
            current.
        """
        if not 1 <= slide_number <= slide_count:
            return False
        if slide_number == self.current_slide_number:
            return False
        self.current_slide_index = slide_number - 1
        self.highlighted_bullet = None
        return True

    def mark_narrated(self, slide_number: int) -> None:
        """Record that a slide has been presented."""
        self.slides_narrated.add(slide_number)

    def log_refusal(self, topic: str) -> None:
        """Record an off-deck question the agent declined to answer."""
        self.refusals.append(RefusalRecord(slide=self.current_slide_number, topic=topic))

    def mark_turn_ended(self) -> None:
        """Record that the user's turn just ended - the zero point for latency logs."""
        self.turn_ended_at = time.monotonic()

    def elapsed_since_turn_end(self) -> float | None:
        """Seconds since the user's turn ended, or None if no turn has ended yet."""
        if self.turn_ended_at is None:
            return None
        return time.monotonic() - self.turn_ended_at


@dataclass
class AppResources:
    """Typed bag handed to ``PipelineWorker(app_resources=...)``.

    Tool handlers read it back with ``cast(AppResources, params.app_resources)``.
    A dataclass rather than a dict so the read sites get autocomplete and
    refactor safety instead of string-key lookups.

    The deck lives here too, so the tools operate on whichever deck this session
    is presenting rather than on a module-level constant.
    """

    state: PresentationState
    deck: Deck
