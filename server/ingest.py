"""Turning an upload into a presentable deck.

    PPTX -> (LibreOffice) -> PDF -> per-page text + rendered PNG
         -> narration model -> deck synthesis -> Deck

Runs as a background task. The deck is written to the store at every step, so
the parsing view can poll and watch it fill in rather than staring at a
spinner. What it publishes, in order:

    phase="converting"   PowerPoint only; see ``convert.py``
    phase="rendering"    page_count as soon as the document opens, then
                         pages_rendered after each page image is on disk
    phase="narrating"    progress, one per slide, as narration lands
    phase="synthesizing" status flips to "ready" here - the deck is already
                         presentable and synthesis is only polish
    phase="done"

A slide whose narration fails gets a placeholder instead of sinking the whole
upload - a deck with one thin slide is still presentable.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable

import pymupdf
from loguru import logger

import store
from convert import ConversionError, pptx_to_pdf
from narration import NarrationModel, build_narration_model, synthesize_deck, title_for_deck
from slides import Deck, IngestPhase, Slide

# Awaited with a count: the page total when the document opens, then the
# running number of pages rendered.
PageCallback = Callable[[int], Awaitable[None]]

MAX_SLIDES = int(os.environ.get("MAX_SLIDES", "40"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
# Slides narrated at once. The bound is the narration API, not the PDF parsing.
CONCURRENCY = int(os.environ.get("NARRATION_CONCURRENCY", "4"))
# Every page is rendered to a PNG: the viewer shows the real slide, not just its
# text. 150dpi is a readable full-screen image and is still small enough to hand
# a vision model without a huge payload.
RENDER_DPI = int(os.environ.get("RENDER_DPI", "150"))


class IngestError(Exception):
    """A problem with the uploaded file that the user can act on."""


def _clean(raw: str) -> str:
    """Tidy a PDF text layer enough for a language model to work with."""
    # PDF extraction hard-wraps mid-sentence; join lines that clearly continue.
    text = re.sub(r"(?<![.!?:;])\n(?=[a-z])", " ", raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _page_text(page: pymupdf.Page) -> str:
    return _clean(page.get_text("text") or "")


def extract_pages(
    pdf_bytes: bytes, *, want_images: bool = True, deck_id: str | None = None
) -> list[tuple[str, bytes | None]]:
    """Extract (text, optional PNG) for each page, capped at MAX_SLIDES.

    When ``deck_id`` is given, each rendered page is also written to disk as the
    slide image the viewer displays.

    Raises:
        IngestError: If the file is not a readable PDF, or has no pages.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise IngestError(f"That does not look like a readable PDF ({exc}).") from exc

    with doc:
        if doc.page_count == 0:
            raise IngestError("That PDF has no pages.")
        if doc.needs_pass:
            raise IngestError("That PDF is password protected.")

        if deck_id:
            store.image_dir(deck_id).mkdir(parents=True, exist_ok=True)

        pages: list[tuple[str, bytes | None]] = []
        for index in range(min(doc.page_count, MAX_SLIDES)):
            page = doc.load_page(index)
            image = None
            if want_images:
                image = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
                if deck_id:
                    store.image_path(deck_id, index + 1).write_bytes(image)
            pages.append((_page_text(page), image))

    return pages


async def extract_pages_streaming(
    pdf_bytes: bytes,
    *,
    deck_id: str,
    on_open: PageCallback | None = None,
    on_page: PageCallback | None = None,
) -> list[tuple[str, bytes | None]]:
    """Extract pages one at a time, reporting progress as each one lands.

    The synchronous :func:`extract_pages` renders every page before returning,
    which means a deck spends its entire render doing nothing observable - no
    page count, no thumbnails, nothing for a progress view to show. This does
    the same work, but announces the page count as soon as the document opens
    and calls back after writing each image, so the UI can fill in real
    thumbnails while the rest still render.

    Each page is rendered in a worker thread. Rasterising is CPU-bound and
    would otherwise stall the event loop for the whole document, which on a
    large deck is long enough to starve the very polling requests that are
    trying to read the progress this function publishes.

    Args:
        pdf_bytes: The source PDF.
        deck_id: Deck to write page images for.
        on_open: Awaited once with the page count, before any rendering.
        on_page: Awaited after each page image is written, with the number of
            pages rendered so far.

    Returns:
        ``(text, png_bytes)`` per page, in page order - the same shape
        :func:`extract_pages` returns.

    Raises:
        IngestError: If the file is not a readable PDF, or has no pages.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise IngestError(f"That does not look like a readable PDF ({exc}).") from exc

    with doc:
        if doc.page_count == 0:
            raise IngestError("That PDF has no pages.")
        if doc.needs_pass:
            raise IngestError("That PDF is password protected.")

        total = min(doc.page_count, MAX_SLIDES)
        if on_open is not None:
            await on_open(total)

        store.image_dir(deck_id).mkdir(parents=True, exist_ok=True)

        def render(index: int) -> tuple[str, bytes]:
            page = doc.load_page(index)
            image = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
            store.image_path(deck_id, index + 1).write_bytes(image)
            return _page_text(page), image

        pages: list[tuple[str, bytes | None]] = []
        for index in range(total):
            text, image = await asyncio.to_thread(render, index)
            pages.append((text, image))
            if on_page is not None:
                await on_page(index + 1)

    return pages


def _placeholder(slide_number: int, text: str) -> Slide:
    """Stand-in for a slide whose narration call failed."""
    snippet = " ".join(text.split())[:240]
    return Slide(
        id=slide_number,
        title=f"Slide {slide_number}",
        bullets=[s for s in (snippet[:80],) if s] or ["No text could be read from this slide"],
        narration=(
            snippet
            or "This slide has no text that could be read, so there is nothing to say about it."
        ),
        keywords=[f"slide {slide_number}"],
    )


async def _narrate_one(
    model: NarrationModel,
    *,
    text: str,
    image: bytes | None,
    number: int,
    total: int,
    deck_title: str,
) -> Slide:
    try:
        result = await model.narrate(
            text=text,
            slide_number=number,
            total=total,
            deck_title=deck_title,
            image_png=image,
        )
    except Exception as exc:
        logger.warning(f"Narration failed for slide {number}: {exc}")
        return _placeholder(number, text)

    return Slide(
        id=number,
        title=result["title"],
        bullets=result["bullets"],
        narration=result["narration"],
        keywords=result["keywords"],
    )


async def ingest_pdf(deck: Deck, pdf_bytes: bytes, *, is_pptx: bool = False) -> None:
    """Ingest an upload into an existing "processing" deck, then mark it ready.

    Writes progress to the store as it goes - phase, page count, pages
    rendered, slides narrated - so the parsing view can show what is actually
    happening rather than an indeterminate spinner. Never raises: a failure is
    recorded on the deck as ``status="failed"`` so the UI can show why.

    Args:
        deck: A deck already saved with ``status="processing"``.
        pdf_bytes: The uploaded file. PDF, or PowerPoint when ``is_pptx``.
        is_pptx: Convert from PowerPoint first. See ``convert.py``.
    """

    async def set_phase(phase: IngestPhase) -> None:
        deck.phase = phase
        await store.save(deck)

    try:
        model = build_narration_model()

        if is_pptx:
            await set_phase("converting")
            try:
                pdf_bytes = await pptx_to_pdf(pdf_bytes, filename=deck.source or "deck.pptx")
            except ConversionError as exc:
                # The user can act on every one of these (install LibreOffice,
                # re-export the file), so surface it as an ingest error rather
                # than a generic crash.
                raise IngestError(str(exc)) from exc

        await set_phase("rendering")

        async def on_open(count: int) -> None:
            deck.page_count = count
            await store.save(deck)

        async def on_page(rendered: int) -> None:
            deck.pages_rendered = rendered
            await store.save(deck)

        pages = await extract_pages_streaming(
            pdf_bytes, deck_id=deck.id, on_open=on_open, on_page=on_page
        )
        total = len(pages)
        deck.has_images = True
        deck.page_count = total
        deck.pages_rendered = total
        await set_phase("narrating")
        logger.info(f"Deck {deck.id}: {total} page(s) extracted from {deck.source!r}")

        if not any(text for text, _ in pages) and not model.wants_image:  # noqa: SIM102
            raise IngestError(
                "No text could be extracted from that PDF - it is probably a scan or "
                "made of images. Configure a vision model (VISION_API_KEY and "
                "VISION_MODEL) to narrate decks like this one."
            )

        deck.title = await title_for_deck(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            first_slides="\n\n".join(t for t, _ in pages[:3]),
            filename=deck.source or "deck.pdf",
        )
        await store.save(deck)

        semaphore = asyncio.Semaphore(CONCURRENCY)
        done = 0
        lock = asyncio.Lock()

        async def worker(index: int) -> Slide:
            nonlocal done
            text, image = pages[index]
            async with semaphore:
                slide = await _narrate_one(
                    model,
                    text=text,
                    image=image if model.wants_image else None,
                    number=index + 1,
                    total=total,
                    deck_title=deck.title,
                )
            # Publish progress as each slide lands, so the upload page can move.
            async with lock:
                done += 1
                deck.progress = done
            await store.save(deck)
            return slide

        slides = await asyncio.gather(*(worker(i) for i in range(total)))

        deck.slides = sorted(slides, key=lambda s: s.id)
        deck.progress = total
        # Ready *before* synthesis: the deck is fully presentable now, and
        # synthesis is polish. Marking it ready here means the gallery opens as
        # soon as there is something to look at.
        deck.status = "ready"
        deck.phase = "synthesizing"
        deck.error = None
        await store.save(deck)
        logger.info(f"Deck {deck.id} ready: {deck.title!r}, {total} slide(s)")

        # Deck-level synthesis: intro, per-slide transitions, outro, persona.
        # Best-effort - a deck without this is still fully presentable, so a
        # failure here is logged and the deck stays ready rather than failing
        # the whole upload over what is polish, not substance.
        try:
            synthesis = await synthesize_deck(
                api_key=os.environ["DEEPSEEK_API_KEY"],
                deck_title=deck.title,
                slides=[
                    {"id": s.id, "title": s.title, "narration": s.narration}
                    for s in deck.slides
                ],
            )
            deck.apply_synthesis(synthesis)
            logger.info(
                f"Deck {deck.id} synthesized: intro + "
                f"{len(synthesis['transitions'])} transition(s)"
            )
        except Exception as exc:
            logger.warning(f"Deck {deck.id} synthesis failed ({exc}); presenting without it")

        # Either way ingestion is over and nothing further will change.
        deck.phase = "done"
        await store.save(deck)

    except IngestError as exc:
        deck.status = "failed"
        deck.phase = "done"
        deck.error = str(exc)
        await store.save(deck)
        logger.warning(f"Deck {deck.id} failed: {exc}")

    except Exception as exc:
        deck.status = "failed"
        deck.phase = "done"
        deck.error = f"Ingestion failed: {exc}"
        await store.save(deck)
        logger.exception(f"Deck {deck.id} failed unexpectedly")
