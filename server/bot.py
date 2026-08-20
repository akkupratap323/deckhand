"""The Pipecat pipeline.

Stack, and why each piece is what it is:

- STT: Deepgram Flux. One model does transcription *and* turn detection, using
  acoustic and semantic cues together, so there is no separate VAD and no
  separate end-of-turn model to disagree with it. Barge-in is native.
- LLM: DeepSeek. Gemini was tried and reverted - see below.
- TTS: Cartesia (``sonic-3``). Not Deepgram Flux TTS, which was the original
  choice - see below.

Deliberately absent: a Silero VAD analyzer for turn boundaries. Flux owns turn
detection; adding a second detector would give the pipeline two opinions about
when the user stopped talking. ``ExternalUserTurnStartStrategy`` and
``ExternalUserTurnStopStrategy`` tell the aggregator to defer to the service's
proposals instead of deciding for itself.

The DeepSeek/Gemini back-and-forth, for whoever finds this confusing in git
blame: DeepSeek's chat model (``deepseek-chat``, which DeepSeek's API
currently resolves to their v4 reasoning model - confirmed by calling it
directly) always spends completion tokens on hidden ``reasoning_content``
before emitting a single token of visible output or a tool call, with no
setting to turn it off - discovered in ``narration.py`` while debugging
truncated narration output. That motivated a move to Gemini, whose thinking is
at least a documented, controllable setting.

It didn't pan out. Gemini 3 models (``gemini-3.6-flash``) have no full "off"
for thinking either - their floor is ``thinking_level="minimal"`` - and a live
call at that setting measured 2.0-2.3s LLM TTFB, not obviously better than
DeepSeek's hidden reasoning pass. ``gemini-2.5-flash`` genuinely can disable
thinking (``thinking_budget=0``) but 404s on this account: "no longer
available to new users." Worse, the free tier caps ``gemini-3.6-flash`` at 20
requests/day/project - confirmed by a live 429 (``RESOURCE_EXHAUSTED``,
``GenerateRequestsPerDayPerProjectPerModel-FreeTier``) that left the app
connecting successfully but silent, because the very first call (the opening
greeting) had nowhere to go. ``narration.py``'s vision path draws from the
same per-model quota, so upload testing and live sessions were competing for
the same 20 requests. Reverted to DeepSeek, which never hit a rate limit or a
model-availability wall all session. Revisit Gemini if billing gets enabled on
that key - the code for it is in git history, not deleted, just not wired in.

Tool-calling itself was verified identical either way and is not part of this
trade-off: ``PRESENTER_TOOLS`` are plain Python functions, and both DeepSeek's
and Gemini's adapters convert them into the same schema with no changes
needed to ``tools.py``.

Why Cartesia over Flux TTS: Flux TTS has no cancel message yet, so Pipecat
fakes interruption by reconnecting its websocket - and does so *before*
forwarding the interruption frame downstream, which was measurable,
user-audible latency on barge-in (traced through Pipecat 1.7.0's source; see
git history for ``fast_interrupt_tts.py``, since removed). Cartesia's service
sends a real in-band cancel over the same open connection - confirmed in
``pipecat.services.cartesia.tts``, no reconnect, no private-API patch needed.
It also gives real per-word timestamps, which Flux TTS's protocol does not.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepseek.llm import DeepSeekLLMService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start import ExternalUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from latency import LatencySpeakingLogger
from net import ipv4_transport, log_transport_choice
from prompts import GREETING_CUE, build_system_prompt
from presenters import Presenter, get as get_presenter, voice_id_for
from slides import Deck
from state import AppResources, PresentationState
from tools import PRESENTER_TOOLS

load_dotenv(override=True)


def _keyterms(deck: Deck, limit: int = 12) -> list[str]:
    """Domain vocabulary to bias the ASR toward, drawn from the deck itself.

    General ASR reliably mangles the terms a deck is actually about - acronyms
    especially. Feeding Flux the deck's own keywords fixes that without
    hardcoding any one subject.
    """
    seen: dict[str, None] = {}
    for slide in deck.slides:
        for term in slide.keywords:
            cleaned = term.strip()
            # Multi-word phrases and single letters are not useful keyterms.
            if 2 < len(cleaned) <= 30 and len(cleaned.split()) <= 2:
                seen.setdefault(cleaned, None)
    return list(seen)[:limit]


class Ipv4DeepSeekLLMService(DeepSeekLLMService):
    """DeepSeek, reached over IPv4 only.

    Pipecat builds the ``AsyncOpenAI`` client with a hardcoded ``http_client``
    and no way to influence it from the constructor, so the only injection
    point is ``create_client``. See ``net.py`` for the NAT64 failure this
    exists to avoid - without it, every call on this network spends its whole
    retry budget on an unroutable address and the agent never speaks.
    """

    def create_client(
        self,
        api_key=None,
        base_url=None,
        organization=None,
        project=None,
        default_headers=None,
        **kwargs,
    ):
        transport = ipv4_transport()
        if transport is None:
            return super().create_client(
                api_key, base_url, organization, project, default_headers, **kwargs
            )
        logger.debug("Creating DeepSeek client pinned to IPv4")
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            project=project,
            # Same limits Pipecat's own create_client uses; only the transport
            # differs. Keep these in step if Pipecat changes them.
            http_client=DefaultAsyncHttpxClient(
                transport=transport,
                limits=httpx.Limits(
                    max_keepalive_connections=100, max_connections=1000, keepalive_expiry=None
                ),
            ),
            default_headers=default_headers,
        )


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back on anything unparseable."""
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        logger.warning(f"{name} is not a number; using {default}")
        return default


async def run_bot(
    webrtc_connection: SmallWebRTCConnection,
    deck: Deck,
    presenter: Presenter | None = None,
) -> None:
    """Run one presentation session, start to finish.

    Args:
        webrtc_connection: An already-negotiated connection from the signalling
            endpoint in ``main.py``.
        deck: The deck to present. Built-in or uploaded; the pipeline does not
            care which.
        presenter: Who presents it - voice and delivery style. Falls back to
            the default presenter when omitted, so callers without a UI to
            choose from (the smoke test) still work.
    """
    speaker = presenter or get_presenter(None)
    logger.info(
        f"Session starting on deck {deck.id!r} ({deck.slide_count} slides) "
        f"as {speaker.name} ({speaker.tagline})"
    )
    log_transport_choice()
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    stt = DeepgramFluxSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        model=os.environ.get("DEEPGRAM_FLUX_STT_MODEL", "flux-general-en"),
        # Barge-in. The user talking over the agent interrupts it.
        should_interrupt=True,
        settings=DeepgramFluxSTTService.Settings(
            # Confidence Flux needs before it calls the turn finished. See the
            # README for how these two numbers were chosen.
            eot_threshold=_env_float("FLUX_EOT_THRESHOLD", 0.7),
            eager_eot_threshold=_env_float("FLUX_EAGER_EOT_THRESHOLD", 0.5),
            eot_timeout_ms=int(_env_float("FLUX_EOT_TIMEOUT_MS", 5000)),
            keyterm=_keyterms(deck),
        ),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=voice_id_for(speaker),
        model=os.environ.get("CARTESIA_MODEL", "sonic-3"),
        # Cartesia's own default is SENTENCE mode, which the service's
        # docstring notes adds ~200-300ms per sentence (it waits for
        # closing punctuation before sending anything to synthesis). TOKEN
        # streams text to Cartesia as the LLM emits it - lower latency,
        # matching the behaviour Flux TTS had. Override via
        # CARTESIA_AGGREGATION_MODE=sentence to compare the two by ear.
        text_aggregation_mode=(
            TextAggregationMode.SENTENCE
            if os.environ.get("CARTESIA_AGGREGATION_MODE", "token").strip().lower() == "sentence"
            else TextAggregationMode.TOKEN
        ),
    )

    llm = Ipv4DeepSeekLLMService(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        settings=DeepSeekLLMService.Settings(
            system_instruction=build_system_prompt(deck, speaker.style),
            # Low, on purpose. The presenter should paraphrase the deck, not
            # improvise around it.
            temperature=0.3,
        ),
    )

    # Direct functions listed in the context register themselves with the LLM.
    context = LLMContext(tools=PRESENTER_TOOLS)

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Defer to Flux for both ends of the turn rather than running a
            # second detector alongside it.
            user_turn_strategies=UserTurnStrategies(
                start=[ExternalUserTurnStartStrategy()],
                stop=[ExternalUserTurnStopStrategy()],
            ),
        ),
    )

    state = PresentationState()
    resources = AppResources(state=state, deck=deck)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            # After transport.output(), not before: BotStartedSpeakingFrame is
            # pushed from there, and this logs against that, not against TTS
            # merely starting to generate. See latency.py.
            LatencySpeakingLogger(state),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        app_resources=resources,
    )

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    rtvi = worker.rtvi

    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi_processor):
        """Hand the browser the deck, then start the talk.

        No ``set_bot_ready()`` here: ``PipelineWorker`` registers its own
        ``on_client_ready`` handler that already sends it. Calling it again sent
        the client two ``bot-ready`` messages.
        """
        await rtvi_processor.send_server_message(
            {
                "type": "presentation-init",
                "payload": {
                    "deck_id": deck.id,
                    "title": deck.title,
                    "slides": deck.public_slides(),
                    "current": state.current_slide_number,
                    "total": deck.slide_count,
                },
            }
        )
        state.mark_narrated(1)
        context.add_message({"role": "developer", "content": GREETING_CUE})
        await worker.queue_frames([LLMRunFrame()])

    # --- Flux turn events -------------------------------------------------
    # Logged rather than acted on. They are what makes the interruption
    # behaviour legible when reading the server output during a demo.

    @stt.event_handler("on_start_of_turn")
    async def on_start_of_turn(service, transcript):
        logger.debug("Flux: user started speaking")

    @stt.event_handler("on_eager_end_of_turn")
    async def on_eager_end_of_turn(service, transcript):
        # Flux predicts the turn is ending before it confirms it. Speculatively
        # starting the LLM call here is real latency removed - see the README
        # section on what is deliberately left undone.
        logger.debug(f"Flux: eager end of turn (unconfirmed) - {transcript!r}")

    @stt.event_handler("on_turn_resumed")
    async def on_turn_resumed(service):
        logger.debug("Flux: user resumed after eager end of turn")

    @stt.event_handler("on_end_of_turn")
    async def on_end_of_turn(service, transcript):
        logger.debug(f"Flux: end of turn confirmed - {transcript!r}")
        # The zero point for every [latency] line that follows - see
        # latency.py and tools.py's _go() for what gets measured from it.
        state.mark_turn_ended()
        logger.info("[latency] turn ended - user finished speaking")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await runner.cancel()

    await runner.run()

    if state.refusals:
        logged = ", ".join(f"{r.topic!r} (on slide {r.slide})" for r in state.refusals)
        logger.info(f"Off-deck questions refused this session: {logged}")
    logger.info(f"Slides presented: {sorted(state.slides_narrated)}")
