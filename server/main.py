"""FastAPI app: deck management, WebRTC signalling, and the built frontend.

The browser uploads a PDF to ``/api/decks``, polls it while it ingests, then
POSTs an SDP offer to ``/api/offer`` naming the deck it wants presented. This
hands back an answer and spawns a bot session bound to that connection.

In development the frontend runs separately on the Vite dev server and talks to
this over CORS. In the container the frontend is prebuilt and served from here,
so there is one origin and one port for WebRTC to worry about.

Everything about the presentation itself lives in ``bot.py``. This file only
gets the two sides talking.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pydantic import BaseModel

import store
from bot import run_bot
from convert import INSTALL_HINT, PPTX_SUFFIXES, soffice_available
from insights import InsightsError, analyze
import presenters
from ingest import MAX_SLIDES, MAX_UPLOAD_MB, ingest_pdf
from slides import DEFAULT_DECK_ID, Deck, new_deck_id

load_dotenv(override=True)

# Live peer connections, keyed by the id the client echoes back on renegotiation.
connections: dict[str, SmallWebRTCConnection] = {}

ICE_SERVERS = [IceServer(urls="stun:stun.l.google.com:19302")]

# Two different things need two different sets of keys, so one flat list
# stopped being accurate once ingestion started optionally using Gemini for
# vision. Both the live session and ingestion currently run on DeepSeek -
# the live LLM was briefly Gemini and got reverted (see bot.py's module
# docstring: a live 429 quota-exhaustion, on top of an unresolved thinking-
# latency question) - but they are kept as separate constants since they are
# conceptually different requirements that happen to currently overlap.
#
# Ingestion (upload -> title, per-slide narration, deck synthesis): DeepSeek
# only. Vision (Gemini/Bedrock/generic) is optional - text narration is the
# fallback, not a hard requirement.
INGEST_REQUIRED_ENV = ("DEEPSEEK_API_KEY",)

# The live voice session: Deepgram for STT, DeepSeek for the conversational
# LLM, Cartesia for TTS.
SESSION_REQUIRED_ENV = ("DEEPGRAM_API_KEY", "DEEPSEEK_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID")


def _has_gemini_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("gemini_api_key"))


def _missing_session_env() -> list[str]:
    """Env vars a live session needs that are not set, in a fixed check order."""
    return [k for k in SESSION_REQUIRED_ENV if not os.environ.get(k)]


def _has_vision_narration() -> bool:
    """Whether build_narration_model() would pick a vision-capable model.

    Mirrors that function's priority order without constructing one - a
    health check should not open a real Bedrock client on every poll.
    """
    if _has_gemini_key():
        return True
    if os.environ.get("BEDROCK_MODEL_ID") and (
        os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("aws_access_key_id")
    ):
        return True
    return bool(os.environ.get("VISION_API_KEY") and os.environ.get("VISION_MODEL"))


class Offer(BaseModel):
    """An SDP offer from the browser, naming the deck to present.

    The deck id can arrive three ways, checked in order by ``_resolve_deck_id``:
    a ``?deck_id=`` query parameter, a top-level field, or nested under
    ``requestData``. The nesting is not a whim - SmallWebRTCTransport puts
    anything you hand it as ``webrtcRequestParams.requestData`` into a
    ``requestData`` key rather than merging it into the body.
    """

    sdp: str
    type: str
    pc_id: str | None = None
    restart_pc: bool = False
    deck_id: str | None = None
    presenter: str | None = None
    requestData: dict[str, Any] | None = None


def _from_offer(request: Offer, query_value: str | None, field: str) -> str | None:
    """Pick a value out of whichever of the three places the client put it.

    Query parameter, then a top-level field, then nested under
    ``requestData`` - the nesting is not a whim, SmallWebRTCTransport puts
    anything handed to it as ``webrtcRequestParams.requestData`` into a
    ``requestData`` key rather than merging it into the body.
    """
    if query_value:
        return query_value
    direct = getattr(request, field, None)
    if isinstance(direct, str) and direct:
        return direct
    if request.requestData:
        nested = request.requestData.get(field)
        if isinstance(nested, str) and nested:
            return nested
    return None


def _resolve_deck_id(request: Offer, query_deck_id: str | None) -> str:
    """Pick the deck id out of whichever place the client put it."""
    return _from_offer(request, query_deck_id, "deck_id") or DEFAULT_DECK_ID


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail loudly at boot if keys are missing, and clean up peers on shutdown."""
    missing = [k for k in INGEST_REQUIRED_ENV if not os.environ.get(k)] + _missing_session_env()
    if missing:
        logger.warning(
            f"Missing environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in - sessions will fail without them."
        )
    store.init()
    yield
    await asyncio.gather(*(c.disconnect() for c in connections.values()), return_exceptions=True)
    connections.clear()


app = FastAPI(title="Voice Presentation Agent", lifespan=lifespan)

# CORS exists only for local development, where Vite serves the frontend from
# a different origin. In production the API and the built frontend are the same
# origin, so nothing needs cross-origin access and the default is to allow none.
#
# `allow_origins=["*"]` with `allow_credentials=True` is the combination to
# avoid: browsers reject it outright, and where it does apply it lets any site
# a user visits drive this API with their cookies. Set CORS_ORIGINS to an
# explicit comma-separated list if a separate frontend origin is ever needed.
_cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    logger.info(f"CORS enabled for: {', '.join(_cors_origins)}")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Liveness plus a check that ingestion and live-session keys are present."""
    return {
        "status": "ok",
        "active_sessions": len(connections),
        "decks": len(store.list_all()),
        "vision_narration": _has_vision_narration(),
        # Lets the upload UI say up front whether .pptx will be accepted.
        "pptx_supported": soffice_available(),
        "configured": {
            "DEEPSEEK_API_KEY": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "GEMINI_API_KEY": _has_gemini_key(),
            "DEEPGRAM_API_KEY": bool(os.environ.get("DEEPGRAM_API_KEY")),
            "CARTESIA_API_KEY": bool(os.environ.get("CARTESIA_API_KEY")),
            "CARTESIA_VOICE_ID": bool(os.environ.get("CARTESIA_VOICE_ID")),
        },
    }


# --- decks -----------------------------------------------------------------


@app.get("/api/decks")
async def list_decks() -> dict[str, Any]:
    """Every deck, newest first, built-in one last."""
    return {"decks": [d.summary() for d in store.list_all()]}


@app.get("/api/decks/{deck_id}")
async def get_deck(deck_id: str) -> dict[str, Any]:
    """One deck, including its slides (titles and bullets, not narration)."""
    deck = store.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="No such deck.")
    return {**deck.summary(), "slides": deck.public_slides()}


@app.get("/api/presenters")
async def list_presenters() -> dict[str, Any]:
    """The presenters a session can be given. Static; safe to cache."""
    return {
        "presenters": [p.public() for p in presenters.PRESENTERS],
        "default": presenters.DEFAULT_PRESENTER_ID,
    }


@app.get("/api/decks/{deck_id}/status")
async def deck_status(deck_id: str) -> dict[str, Any]:
    """Ingestion progress. Polled by the upload page while a deck processes."""
    deck = store.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="No such deck.")
    return {
        "id": deck.id,
        "status": deck.status,
        "error": deck.error,
        "progress": deck.progress,
        "slide_count": deck.slide_count,
        "title": deck.title,
        # The parsing view drives its whole display off these three.
        "phase": deck.phase,
        "page_count": deck.page_count,
        "pages_rendered": deck.pages_rendered,
        "has_images": deck.has_images,
    }


@app.get("/api/decks/{deck_id}/slides/{slide_number}/image")
async def slide_image(deck_id: str, slide_number: int) -> FileResponse:
    """The rendered page image for one slide.

    This is what the viewer displays - the actual slide, not a reconstruction of
    it from extracted text.
    """
    deck = store.get(deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="No such deck.")
    if deck.get_slide(slide_number) is None:
        raise HTTPException(status_code=404, detail="No such slide.")

    path = store.image_path(deck_id, slide_number)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="This deck has no rendered images.")

    return FileResponse(
        path,
        media_type="image/png",
        # Immutable once written: the deck id and slide number fully determine it.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/api/decks", status_code=201)
async def upload_deck(
    background_tasks: BackgroundTasks, file: UploadFile = File(...)
) -> dict[str, Any]:
    """Accept a PDF or PowerPoint file and start ingesting it in the background.

    Returns immediately with a deck in "processing"; the client polls
    ``/api/decks/{id}/status`` from there and watches it parse.
    """
    missing = [k for k in INGEST_REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Server is missing required environment variables: {', '.join(missing)}",
        )

    filename = file.filename or "deck.pdf"
    lower = filename.lower()

    is_pptx = lower.endswith(PPTX_SUFFIXES)
    if is_pptx and not soffice_available():
        # Fail here rather than after the upload has been accepted: the user
        # can act on this immediately, and a deck that dies during ingestion
        # for an environment reason is a worse experience than a clean refusal.
        raise HTTPException(status_code=415, detail=INSTALL_HINT)
    if not is_pptx and not lower.endswith(".pdf"):
        raise HTTPException(
            status_code=415, detail="Only PDF and PowerPoint (.pptx, .ppt) files are supported."
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="That file is empty.")
    if len(payload) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"That file is larger than the {MAX_UPLOAD_MB} MB limit.",
        )

    deck = Deck(
        id=new_deck_id(),
        title=filename.rsplit(".", 1)[0][:120] or "Untitled deck",
        slides=[],
        status="processing",
        source=filename,
    )
    await store.save(deck)

    background_tasks.add_task(ingest_pdf, deck, payload, is_pptx=is_pptx)
    logger.info(f"Accepted {filename!r} ({len(payload) / 1024:.0f} KB) as deck {deck.id}")

    return {**deck.summary(), "max_slides": MAX_SLIDES}


class TranscriptTurn(BaseModel):
    """One spoken turn, as the browser recorded it."""

    role: str
    text: str


class InsightsRequest(BaseModel):
    """A finished conversation, submitted for analysis.

    The transcript comes from the browser rather than the server because the
    server does not keep one: the pipeline is streaming and stateless past the
    LLM context, which is discarded when the session ends. The browser is the
    only place the full exchange exists as text.
    """

    deck_id: str
    turns: list[TranscriptTurn]


@app.post("/api/insights")
async def session_insights(request: InsightsRequest) -> dict[str, Any]:
    """Evaluate a finished conversation: intent, answer quality, deck gaps.

    Nothing is stored. This is a pure function of the transcript sent in, so
    re-running it on the same conversation gives a fresh read rather than a
    cached one.
    """
    missing = [k for k in INGEST_REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Server is missing required environment variables: {', '.join(missing)}",
        )

    deck = store.get(request.deck_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="No such deck.")

    try:
        return await analyze(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            deck_title=deck.title,
            turns=[t.model_dump() for t in request.turns],
            # Given the deck's outline, the model can tell a correct refusal
            # from a dodge instead of guessing what the deck covered.
            slide_titles=[s.title for s in deck.slides],
        )
    except InsightsError as exc:
        # These are all conditions the user can understand and act on
        # (nothing was asked, the session was too long), not server faults.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/decks/{deck_id}", status_code=204)
async def delete_deck(deck_id: str) -> None:
    """Remove an uploaded deck. The built-in deck cannot be deleted."""
    if deck_id == DEFAULT_DECK_ID:
        raise HTTPException(status_code=400, detail="The built-in deck cannot be deleted.")
    if not store.delete(deck_id):
        raise HTTPException(status_code=404, detail="No such deck.")


# --- WebRTC signalling ------------------------------------------------------


@app.post("/api/offer")
async def offer(
    request: Offer,
    background_tasks: BackgroundTasks,
    deck_id: str | None = None,
    presenter: str | None = None,
) -> dict[str, Any]:
    """Answer an SDP offer, and start a bot for new connections.

    A request carrying a known ``pc_id`` is a renegotiation of a live session -
    it reuses that connection and does not spawn a second bot.
    """
    if request.pc_id and request.pc_id in connections:
        connection = connections[request.pc_id]
        logger.info(f"Renegotiating existing connection {request.pc_id}")
        await connection.renegotiate(
            sdp=request.sdp, type=request.type, restart_pc=request.restart_pc
        )
        return connection.get_answer()

    missing = _missing_session_env()
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Server is missing required environment variables: {', '.join(missing)}",
        )

    wanted = _resolve_deck_id(request, deck_id)
    deck = store.get(wanted)
    if deck is None:
        raise HTTPException(status_code=404, detail=f"No such deck: {wanted}")
    if deck.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"That deck is not ready to present (status: {deck.status}).",
        )
    if not deck.slides:
        raise HTTPException(status_code=409, detail="That deck has no slides.")

    connection = SmallWebRTCConnection(ICE_SERVERS)
    await connection.initialize(sdp=request.sdp, type=request.type)

    @connection.event_handler("closed")
    async def on_closed(conn: SmallWebRTCConnection):
        logger.info(f"Connection closed: {conn.pc_id}")
        connections.pop(conn.pc_id, None)

    answer = connection.get_answer()
    connections[answer["pc_id"]] = connection
    # An unknown or missing presenter falls back rather than failing the
    # session - see presenters.get().
    speaker = presenters.get(_from_offer(request, presenter, "presenter"))
    background_tasks.add_task(run_bot, connection, deck, speaker)
    logger.info(f"New session {answer['pc_id']} presenting deck {deck.id}")
    return answer


# --- static frontend -------------------------------------------------------
# Mounted last so it never shadows /api/*. Absent in local development, where
# Vite serves the frontend on its own port instead.

WEB_DIST = Path(os.environ.get("WEB_DIST", Path(__file__).parent.parent / "web" / "dist"))

if (WEB_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        """Serve the built single-page app."""
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(WEB_DIST / "favicon.svg")

    logger.info(f"Serving the built frontend from {WEB_DIST}")
else:
    logger.info(
        f"No built frontend at {WEB_DIST} - API only. "
        "Run the Vite dev server for the UI, or build the container image."
    )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "7860")),
    )
