"""The tools the presenter can call.

These are Pipecat *direct functions*: the schema the model sees is generated
from the signature and the docstring, so the type hints and the ``Args:`` block
below are load-bearing. Editing the wording changes what the model is told.

Two things every navigation tool does, in this order:

1. Push the slide change to the frontend immediately, as a system frame, before
   the model has generated a single spoken token. The screen therefore updates
   while the answer is still being formed rather than after it is spoken.
2. Return the target slide's full content as the tool result. That result is
   what the model then answers from, which is what makes the answer grounded in
   the deck rather than in whatever it remembers about clinical trials.
"""

from __future__ import annotations

from typing import Any, cast

from loguru import logger
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.llm_service import FunctionCallParams

from slides import Deck
from state import AppResources, PresentationState


async def _emit(params: FunctionCallParams, message_type: str, payload: dict[str, Any]) -> None:
    """Send a message to the browser over the RTVI server-message channel.

    ``RTVIServerMessageFrame`` is a system frame, so it overtakes the queued TTS
    audio instead of waiting behind it - that is what makes the slide change
    land before the spoken answer rather than after it. The frontend receives it
    on ``onServerMessage``.
    """
    frame = RTVIServerMessageFrame(data={"type": message_type, "payload": payload})
    await params.llm.push_frame(frame)


def _resources(params: FunctionCallParams) -> AppResources:
    """Read the worker's app resources: this session's state and deck."""
    return cast(AppResources, params.app_resources)


def _state(params: FunctionCallParams) -> PresentationState:
    """The shared presentation state."""
    return _resources(params).state


def _deck(params: FunctionCallParams) -> Deck:
    """The deck this session is presenting."""
    return _resources(params).deck


async def _go(params: FunctionCallParams, slide_number: int, reason: str) -> dict[str, Any]:
    """Move to a slide, push the change, and return the slide as the tool result.

    Shared by every navigation tool so there is one code path that can change
    what is on screen.
    """
    deck = _deck(params)
    slide = deck.get_slide(slide_number)
    if slide is None:
        logger.warning(f"Rejected out-of-range slide {slide_number}")
        return {
            "error": (
                f"Slide {slide_number} does not exist. "
                f"The deck has slides 1 to {deck.slide_count}."
            ),
            "current_slide": _state(params).current_slide_number,
        }

    state = _state(params)
    changed = state.set_slide(slide_number, deck.slide_count)
    state.mode = "answering" if reason == "answer_question" else "presenting"
    state.mark_narrated(slide_number)

    # Push first, answer second.
    await _emit(
        params,
        "slide-change",
        {
            "slide": slide_number,
            "reason": reason,
            "title": slide.title,
            "total": deck.slide_count,
        },
    )
    logger.info(f"slide -> {slide_number} ({slide.title}) reason={reason} changed={changed}")
    elapsed = state.elapsed_since_turn_end()
    if elapsed is not None:
        logger.info(
            f"[latency] slide -> {slide_number} after {elapsed:.2f}s "
            "(since user finished speaking)"
        )

    return slide.to_tool_result()


async def goto_slide(params: FunctionCallParams, slide_number: int, reason: str):
    """Put a specific slide on screen and read its content.

    Call this BEFORE answering whenever the answer lives on a slide that is not
    currently showing. The audience should always be looking at the slide you are
    talking about. The returned content is what you answer from.

    Args:
        slide_number: The slide to show. Slides are numbered from 1.
        reason: Why you are moving. Use "answer_question" when jumping to answer
            something the audience asked, "navigation" when they explicitly asked
            to move, or "presenting" for the normal forward flow of the talk.
    """
    await params.result_callback(await _go(params, slide_number, reason))


async def next_slide(params: FunctionCallParams):
    """Advance to the next slide and read its content.

    Use this when the audience says "next", "go on", or "continue". Returns the
    new slide's content for you to present.
    """
    state = _state(params)
    target = state.current_slide_number + 1
    if target > _deck(params).slide_count:
        await params.result_callback(
            {
                "error": "Already on the last slide.",
                "current_slide": state.current_slide_number,
                "hint": "Follow the <closing> instructions in your system prompt now.",
            }
        )
        return
    await params.result_callback(await _go(params, target, "presenting"))


async def previous_slide(params: FunctionCallParams):
    """Go back to the previous slide and read its content.

    Use this when the audience says "go back" or "previous". Returns the slide's
    content for you to speak from.
    """
    state = _state(params)
    target = state.current_slide_number - 1
    if target < 1:
        await params.result_callback(
            {
                "error": "Already on the first slide.",
                "current_slide": state.current_slide_number,
            }
        )
        return
    await params.result_callback(await _go(params, target, "navigation"))


async def get_current_slide(params: FunctionCallParams):
    """Read the slide currently on screen, without changing it.

    Use this if you have lost track of where you are in the deck.
    """
    state = _state(params)
    deck = _deck(params)
    slide = deck.get_slide(state.current_slide_number)
    assert slide is not None  # current_slide_number is always in range
    result = slide.to_tool_result()
    result["total_slides"] = deck.slide_count
    await params.result_callback(result)


async def highlight_bullet(params: FunctionCallParams, bullet_index: int):
    """Highlight one bullet on the slide currently on screen.

    Call this as you reach the point a specific bullet makes, so the audience can
    see which line you are on. Use it once or twice per slide, not for every
    sentence.

    Args:
        bullet_index: Which bullet to highlight, 0-indexed. The first bullet on
            the slide is 0.
    """
    state = _state(params)
    slide = _deck(params).get_slide(state.current_slide_number)
    assert slide is not None

    if not 0 <= bullet_index < len(slide.bullets):
        await params.result_callback(
            {
                "error": (
                    f"Slide {slide.id} has {len(slide.bullets)} bullets, "
                    f"so valid indexes are 0 to {len(slide.bullets) - 1}."
                )
            }
        )
        return

    state.highlighted_bullet = bullet_index
    await _emit(
        params,
        "highlight",
        {"slide": slide.id, "bullet": bullet_index, "text": slide.bullets[bullet_index]},
    )
    logger.info(f"highlight -> slide {slide.id} bullet {bullet_index}")
    await params.result_callback({"highlighted": slide.bullets[bullet_index]})


async def note_off_deck_question(params: FunctionCallParams, topic: str):
    """Record that the audience asked something this deck does not cover.

    Call this whenever you decline a question for being off-deck, at the same
    time as you say so out loud. It only logs the refusal - you still have to
    deliver it in your own words.

    Args:
        topic: A few words describing what they asked about, for the log.
    """
    state = _state(params)
    state.log_refusal(topic)
    await _emit(params, "refusal", {"topic": topic, "slide": state.current_slide_number})
    logger.info(f"refusal logged: {topic!r}")
    await params.result_callback(
        {
            "logged": True,
            "reminder": (
                "Say plainly that this deck does not cover that, name what it does cover, "
                "and offer to go over any of it. Do not answer the question from general "
                "knowledge."
            ),
        }
    )


PRESENTER_TOOLS = [
    goto_slide,
    next_slide,
    previous_slide,
    get_current_slide,
    highlight_bullet,
    note_off_deck_question,
]
