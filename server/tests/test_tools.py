"""Behavioural tests for the navigation tools.

These cover the graded mechanism: that a tool call moves backend state, pushes
the change to the frontend, and returns the target slide's content for the model
to answer from. Run with ``pytest`` from the ``server/`` directory.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402
from loguru import logger  # noqa: E402
from slides import builtin_deck  # noqa: E402
from state import AppResources, PresentationState  # noqa: E402


class Harness:
    """Stands in for the pipeline: captures pushed frames and tool results."""

    def __init__(self):
        self.state = PresentationState()
        self.deck = builtin_deck()
        self.resources = AppResources(state=self.state, deck=self.deck)
        self.emitted: list[dict] = []
        self.results: list[dict] = []

    def params(self):
        """Build a FunctionCallParams-shaped object for one tool call."""
        harness = self

        class FakeLLM:
            async def push_frame(self, frame):
                harness.emitted.append(frame.data)

        p = types.SimpleNamespace()
        p.llm = FakeLLM()
        p.app_resources = self.resources

        async def result_callback(result):
            harness.results.append(result)

        p.result_callback = result_callback
        return p

    @property
    def last_emit(self) -> dict:
        return self.emitted[-1]

    @property
    def last_result(self) -> dict:
        return self.results[-1]


@pytest.fixture
def h() -> Harness:
    return Harness()


# --- the core mechanism ---------------------------------------------------


async def test_goto_slide_moves_state_pushes_and_grounds(h: Harness):
    """One call must do all three things the design depends on."""
    await tools.goto_slide(h.params(), 5, "answer_question")

    assert h.state.current_slide_number == 5
    assert h.state.mode == "answering"

    # Pushed to the browser...
    assert h.last_emit["type"] == "slide-change"
    assert h.last_emit["payload"]["slide"] == 5
    assert h.last_emit["payload"]["reason"] == "answer_question"

    # ...and returned to the model as grounding.
    assert h.last_result["slide_number"] == 5
    assert "one thousand to three thousand" in h.last_result["content"]


async def test_push_precedes_result(h: Harness):
    """The frontend must be told before the model gets anything to say."""
    await tools.goto_slide(h.params(), 4, "answer_question")
    assert h.emitted and h.results, "expected both a push and a result"
    # The emit list gained its entry during the call, before result_callback ran.
    assert len(h.emitted) == 1 and len(h.results) == 1


async def test_navigation_reason_sets_presenting_mode(h: Harness):
    await tools.goto_slide(h.params(), 2, "navigation")
    assert h.state.mode == "presenting"


# --- bounds ---------------------------------------------------------------


async def test_out_of_range_slide_is_rejected_without_moving(h: Harness):
    await tools.goto_slide(h.params(), 1, "navigation")
    h.emitted.clear()

    await tools.goto_slide(h.params(), 99, "navigation")

    assert "error" in h.last_result
    assert h.state.current_slide_number == 1
    assert not h.emitted, "a rejected navigation must not push a slide change"


async def test_next_clamps_at_end_of_deck(h: Harness):
    await tools.goto_slide(h.params(), 6, "presenting")
    await tools.next_slide(h.params())

    assert "error" in h.last_result
    assert h.state.current_slide_number == 6


async def test_previous_clamps_at_start_of_deck(h: Harness):
    await tools.previous_slide(h.params())

    assert "error" in h.last_result
    assert h.state.current_slide_number == 1


async def test_next_and_previous_step_one_at_a_time(h: Harness):
    await tools.goto_slide(h.params(), 3, "presenting")
    await tools.next_slide(h.params())
    assert h.state.current_slide_number == 4
    await tools.previous_slide(h.params())
    assert h.state.current_slide_number == 3


# --- highlighting ---------------------------------------------------------


async def test_highlight_bullet_sets_state_and_pushes(h: Harness):
    await tools.highlight_bullet(h.params(), 2)

    assert h.state.highlighted_bullet == 2
    assert h.last_emit["type"] == "highlight"
    assert h.last_emit["payload"]["bullet"] == 2


async def test_highlight_out_of_range_is_rejected(h: Harness):
    await tools.highlight_bullet(h.params(), 99)

    assert "error" in h.last_result
    assert h.state.highlighted_bullet is None


async def test_slide_change_clears_stale_highlight(h: Harness):
    """A highlight only ever refers to the slide that was showing when it was set."""
    await tools.highlight_bullet(h.params(), 1)
    assert h.state.highlighted_bullet == 1

    await tools.goto_slide(h.params(), 3, "navigation")
    assert h.state.highlighted_bullet is None


# --- grounding ------------------------------------------------------------


async def test_refusal_is_logged_and_surfaced(h: Harness):
    await tools.note_off_deck_question(h.params(), "price of a specific drug")

    assert len(h.state.refusals) == 1
    assert h.state.refusals[0].topic == "price of a specific drug"
    assert h.last_emit["type"] == "refusal"
    # The tool result has to steer the model away from answering anyway.
    assert "not answer" in h.last_result["reminder"].lower() or (
        "do not answer" in h.last_result["reminder"].lower()
    )


async def test_get_current_slide_does_not_move(h: Harness):
    await tools.goto_slide(h.params(), 4, "presenting")
    h.emitted.clear()

    await tools.get_current_slide(h.params())

    assert h.state.current_slide_number == 4
    assert h.last_result["slide_number"] == 4
    assert not h.emitted


# --- latency instrumentation -----------------------------------------------


async def test_no_turn_marked_yet_elapsed_is_none(h: Harness):
    """Before the first turn ends there is nothing to measure yet."""
    assert h.state.elapsed_since_turn_end() is None


async def test_elapsed_since_turn_end_counts_up(h: Harness):
    h.state.mark_turn_ended()
    first = h.state.elapsed_since_turn_end()
    second = h.state.elapsed_since_turn_end()

    assert first is not None
    assert first >= 0
    assert second >= first, "a later read must never report less elapsed time"


@pytest.fixture
def logged():
    """Capture loguru output as plain strings.

    loguru writes straight to stderr rather than through stdlib logging, so
    pytest's caplog fixture never sees it - it would otherwise report a
    trivial, wrong pass (nothing captured, "not found" always true) rather
    than a real assertion. This adds a real sink for the duration of one test.
    """
    lines: list[str] = []
    handler_id = logger.add(lines.append, format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


async def test_goto_slide_logs_latency_only_after_a_turn_ended(h: Harness, logged):
    """No turn has ended in this harness, so no [latency] line should appear -
    proves the logging is conditional, not printed unconditionally with a
    bogus number."""
    await tools.goto_slide(h.params(), 3, "answer_question")

    assert not any("[latency]" in line for line in logged)


async def test_goto_slide_logs_latency_once_a_turn_has_ended(h: Harness, logged):
    h.state.mark_turn_ended()
    await tools.goto_slide(h.params(), 3, "answer_question")

    latency_lines = [line for line in logged if "[latency]" in line]
    assert len(latency_lines) == 1
    assert "slide -> 3" in latency_lines[0]
