"""Server-side latency instrumentation for the two symptoms that actually
matter to someone talking to this thing: how long until the slide changes,
and how long until the agent starts speaking.

Pipecat's own metrics (``PipelineParams(enable_metrics=True)``) already log
each service's own processing time - LLM time-to-first-byte, TTS
time-to-first-audio - but every one of those is scoped to that service alone,
buried in DEBUG output alongside everything else, and none of them answer the
one question a listener actually experiences: *how long was it silent after I
stopped talking?* That spans STT turn confirmation, LLM decision time, and TTS
startup together, and nothing reports it as one figure. This does, at INFO
level, with a ``[latency]`` prefix so it is easy to grep out of the noise:

    docker compose logs -f | grep latency
    # or, running the server directly:
    python main.py | grep latency

Three lines, one per turn:

    [latency] turn ended - user finished speaking
    [latency] slide -> 5 after 1.42s (since user finished speaking)
    [latency] agent started speaking after 2.87s (since user finished speaking)

The first is logged by ``bot.py``'s ``on_end_of_turn`` handler, which sets the
zero point (``PresentationState.mark_turn_ended``). The second is logged by
``tools.py`` when a navigation tool fires. The third is this module's job.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import BotStartedSpeakingFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from state import PresentationState


class LatencySpeakingLogger(FrameProcessor):
    """Logs the delay between the user's turn ending and the agent's first
    audible word.

    Placed after ``transport.output()`` in the pipeline:
    ``BotStartedSpeakingFrame`` is pushed from there
    (``pipecat.transports.base_output``), which is the earliest point that
    reflects audio actually being sent to the client - not just the TTS
    service starting to generate it, which would undercount whatever
    buffering happens after that.
    """

    def __init__(self, state: PresentationState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            elapsed = self._state.elapsed_since_turn_end()
            if elapsed is not None:
                logger.info(
                    f"[latency] agent started speaking after {elapsed:.2f}s "
                    "(since user finished speaking)"
                )
            # Only the first BotStartedSpeakingFrame after a turn is the
            # measurement that matters - clear the zero point so a later
            # speaking segment in the same turn (or the next turn, before its
            # own on_end_of_turn fires) does not log a stale, misleading number.
            self._state.turn_ended_at = None

        await self.push_frame(frame, direction)
