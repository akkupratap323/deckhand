"""Tests for the progress ingestion publishes while it runs.

The parsing view is only as good as what the backend tells it, and the thing
that makes that view feel live is that page images land on disk *during*
rendering rather than all at once at the end. These tests pin that down: the
page count is known before any page renders, thumbnails become fetchable one
at a time, and the phase never lies about what is happening.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest  # noqa: E402
import store  # noqa: E402
from slides import Deck  # noqa: E402


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DECKS_DIR", tmp_path / "decks")
    return tmp_path


def _make_pdf(page_count: int) -> bytes:
    doc = pymupdf.open()
    for i in range(page_count):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {i + 1} content", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


# --- streaming extraction --------------------------------------------------


async def test_page_count_is_reported_before_any_page_renders(temp_store):
    """The view lays out its slots up front, so the count must come first."""
    order: list[str] = []

    async def on_open(count: int) -> None:
        order.append(f"open:{count}")

    async def on_page(done: int) -> None:
        order.append(f"page:{done}")

    await ingest.extract_pages_streaming(
        _make_pdf(3), deck_id="d1", on_open=on_open, on_page=on_page
    )

    assert order == ["open:3", "page:1", "page:2", "page:3"]


async def test_each_image_is_on_disk_by_the_time_it_is_announced(temp_store):
    """A thumbnail the UI is told about must actually be fetchable."""
    seen: list[int] = []

    async def on_page(done: int) -> None:
        # The callback fires for page `done`; that file must already exist,
        # otherwise the UI would request a 404 the moment the count moves.
        assert store.image_path("d2", done).is_file()
        seen.append(done)

    await ingest.extract_pages_streaming(_make_pdf(4), deck_id="d2", on_page=on_page)
    assert seen == [1, 2, 3, 4]


async def test_streaming_returns_the_same_shape_as_the_sync_version(temp_store):
    pdf = _make_pdf(2)
    streamed = await ingest.extract_pages_streaming(pdf, deck_id="d3")
    direct = ingest.extract_pages(pdf, want_images=True)

    assert len(streamed) == len(direct) == 2
    for (s_text, s_img), (d_text, d_img) in zip(streamed, direct, strict=True):
        assert s_text == d_text
        assert s_img is not None and d_img is not None


async def test_streaming_respects_the_slide_cap(temp_store, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_SLIDES", 2)
    counts: list[int] = []

    async def on_open(count: int) -> None:
        counts.append(count)

    pages = await ingest.extract_pages_streaming(
        _make_pdf(5), deck_id="d4", on_open=on_open
    )
    # The announced count must be the capped one, not the document's own.
    assert counts == [2]
    assert len(pages) == 2


async def test_streaming_rejects_a_non_pdf(temp_store):
    with pytest.raises(ingest.IngestError):
        await ingest.extract_pages_streaming(b"not a pdf at all", deck_id="d5")


# --- phases through a whole ingest ----------------------------------------


@pytest.fixture
def stub_narration(monkeypatch):
    """Replace every network call in ingestion with something instant."""

    class Model:
        wants_image = False

        async def narrate(self, *, text, slide_number, total, deck_title, image_png=None):
            return {
                "title": f"Slide {slide_number}",
                "bullets": ["a", "b"],
                "narration": "Spoken words.",
                "keywords": ["k"],
            }

    async def fake_title(**kwargs):
        return "A Title"

    async def fake_synthesis(**kwargs):
        return {"intro": "Hi.", "outro": "Bye.", "persona": "A presenter.", "transitions": {}}

    monkeypatch.setattr(ingest, "build_narration_model", lambda: Model())
    monkeypatch.setattr(ingest, "title_for_deck", fake_title)
    monkeypatch.setattr(ingest, "synthesize_deck", fake_synthesis)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


async def test_phases_run_in_order(temp_store, stub_narration, monkeypatch):
    phases: list[str] = []
    real_save = store.save

    async def spy(deck):
        phases.append(deck.phase)
        await real_save(deck)

    monkeypatch.setattr(store, "save", spy)

    deck = Deck(id="p1", title="x", status="processing", phase="queued")
    await ingest.ingest_pdf(deck, _make_pdf(2))

    # Deduplicate consecutive repeats - what matters is the sequence, not how
    # many times each phase saved.
    ordered = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
    assert ordered == ["rendering", "narrating", "synthesizing", "done"]
    assert deck.status == "ready"


async def test_pptx_adds_a_converting_phase_first(temp_store, stub_narration, monkeypatch):
    phases: list[str] = []
    real_save = store.save

    async def spy(deck):
        phases.append(deck.phase)
        await real_save(deck)

    async def fake_convert(data, *, filename="deck.pptx"):
        return _make_pdf(1)

    monkeypatch.setattr(store, "save", spy)
    monkeypatch.setattr(ingest, "pptx_to_pdf", fake_convert)

    deck = Deck(id="p2", title="x", status="processing", phase="queued", source="deck.pptx")
    await ingest.ingest_pdf(deck, b"fake pptx bytes", is_pptx=True)

    assert phases[0] == "converting"
    assert deck.status == "ready"


async def test_conversion_failure_fails_the_deck_with_its_message(
    temp_store, stub_narration, monkeypatch
):
    async def boom(data, *, filename="deck.pptx"):
        raise ingest.ConversionError("LibreOffice is not installed anywhere useful.")

    monkeypatch.setattr(ingest, "pptx_to_pdf", boom)

    deck = Deck(id="p3", title="x", status="processing", source="deck.pptx")
    await ingest.ingest_pdf(deck, b"bytes", is_pptx=True)

    assert deck.status == "failed"
    # The user needs the actionable reason, not a generic "ingestion failed".
    assert "LibreOffice" in (deck.error or "")
    # And the phase must not be left claiming work is still in flight.
    assert deck.phase == "done"


async def test_counts_are_published_during_ingest(temp_store, stub_narration):
    deck = Deck(id="p4", title="x", status="processing")
    await ingest.ingest_pdf(deck, _make_pdf(3))

    assert deck.page_count == 3
    assert deck.pages_rendered == 3
    assert deck.progress == 3
    assert deck.has_images is True


async def test_deck_is_ready_before_synthesis_finishes(temp_store, stub_narration, monkeypatch):
    """The gallery should open as soon as slides exist, not after polish."""
    status_when_synthesis_ran: list[str] = []

    async def slow_synthesis(**kwargs):
        loaded = store.get("p5")
        status_when_synthesis_ran.append(loaded.status if loaded else "missing")
        return {"intro": "", "outro": "", "persona": "", "transitions": {}}

    monkeypatch.setattr(ingest, "synthesize_deck", slow_synthesis)

    deck = Deck(id="p5", title="x", status="processing")
    await ingest.ingest_pdf(deck, _make_pdf(1))

    assert status_when_synthesis_ran == ["ready"]


async def test_failed_synthesis_still_leaves_a_ready_deck(temp_store, stub_narration, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(ingest, "synthesize_deck", boom)

    deck = Deck(id="p6", title="x", status="processing")
    await ingest.ingest_pdf(deck, _make_pdf(1))

    assert deck.status == "ready"
    assert deck.phase == "done"


# --- deleting a deck mid-ingest -------------------------------------------


@pytest.fixture
def fresh_deletes(monkeypatch):
    monkeypatch.setattr(store, "_deleted", set())


async def test_delete_during_narration_stops_the_ingest(
    temp_store, stub_narration, fresh_deletes, monkeypatch
):
    """The deck stays gone, and the slides still queued are not narrated."""
    narrated: list[int] = []
    synthesized: list[bool] = []

    class Model:
        wants_image = False

        async def narrate(self, *, text, slide_number, total, deck_title, image_png=None):
            narrated.append(slide_number)
            if slide_number == 1:
                assert store.delete("x1") is True
            return {"title": "T", "bullets": ["a"], "narration": "N.", "keywords": []}

    async def fake_synthesis(**kwargs):
        synthesized.append(True)
        return {"intro": "", "outro": "", "persona": "", "transitions": {}}

    monkeypatch.setattr(ingest, "build_narration_model", lambda: Model())
    monkeypatch.setattr(ingest, "synthesize_deck", fake_synthesis)
    monkeypatch.setattr(ingest, "CONCURRENCY", 1)

    deck = Deck(id="x1", title="x", status="processing")
    await store.save(deck)
    await ingest.ingest_pdf(deck, _make_pdf(4))  # must not raise

    assert store.get("x1") is None
    assert not (temp_store / "decks" / "x1.json").exists()
    assert not store.image_dir("x1").exists()
    assert narrated == [1]
    assert synthesized == []


async def test_delete_during_rendering_stops_the_ingest(
    temp_store, stub_narration, fresh_deletes, monkeypatch
):
    real_save = store.save
    narrated: list[int] = []

    async def spy(deck):
        if deck.pages_rendered == 1 and not store.was_deleted(deck.id):
            assert store.delete(deck.id) is True
        await real_save(deck)

    class Model:
        wants_image = False

        async def narrate(self, *, text, slide_number, total, deck_title, image_png=None):
            narrated.append(slide_number)
            return {"title": "T", "bullets": ["a"], "narration": "N.", "keywords": []}

    monkeypatch.setattr(store, "save", spy)
    monkeypatch.setattr(ingest, "build_narration_model", lambda: Model())

    deck = Deck(id="x2", title="x", status="processing")
    await real_save(deck)
    await ingest.ingest_pdf(deck, _make_pdf(3))

    assert store.get("x2") is None
    assert not store.image_dir("x2").exists()
    assert deck.pages_rendered == 1
    assert narrated == []


async def test_a_page_written_during_the_delete_is_cleaned_up(
    temp_store, stub_narration, fresh_deletes, monkeypatch
):
    """The delete can miss a page the renderer finishes at the same moment."""
    real_save = store.save

    async def spy(deck):
        if deck.pages_rendered == 1 and not store.was_deleted(deck.id):
            assert store.delete(deck.id) is True
            # Stands in for that page, landing after the delete's cleanup.
            store.image_dir(deck.id).mkdir(parents=True, exist_ok=True)
            store.image_path(deck.id, 2).write_bytes(b"\x89PNG")
        await real_save(deck)

    monkeypatch.setattr(store, "save", spy)

    deck = Deck(id="x5", title="x", status="processing")
    await real_save(deck)
    await ingest.ingest_pdf(deck, _make_pdf(3))

    assert store.get("x5") is None
    assert not store.image_dir("x5").exists()


async def test_delete_after_ready_keeps_the_deck_deleted(
    temp_store, stub_narration, fresh_deletes, monkeypatch
):
    """Deleting a ready deck while synthesis runs must not undo the delete."""

    async def deleting_synthesis(**kwargs):
        assert store.delete("x3") is True
        return {"intro": "Hi.", "outro": "", "persona": "", "transitions": {}}

    monkeypatch.setattr(ingest, "synthesize_deck", deleting_synthesis)

    deck = Deck(id="x3", title="x", status="processing")
    await store.save(deck)
    await ingest.ingest_pdf(deck, _make_pdf(2))

    assert store.get("x3") is None
    assert not store.image_dir("x3").exists()


async def test_a_failure_after_delete_is_not_written_back(
    temp_store, stub_narration, fresh_deletes, monkeypatch
):
    """The failure handlers save too; they must not recreate a deleted deck."""

    async def failing_title(**kwargs):
        store.delete("x4")
        raise RuntimeError("title call failed")

    monkeypatch.setattr(ingest, "title_for_deck", failing_title)

    deck = Deck(id="x4", title="x", status="processing")
    await store.save(deck)
    await ingest.ingest_pdf(deck, _make_pdf(2))

    assert store.get("x4") is None
    assert not store.image_dir("x4").exists()
