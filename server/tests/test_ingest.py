"""Tests for deck storage, PDF extraction, and narration parsing.

The narration model itself is not called here - these cover the code around it,
which is where the failure modes that would corrupt a deck actually live.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest  # noqa: E402
import narration  # noqa: E402
import store  # noqa: E402
from slides import Deck, Slide, builtin_deck  # noqa: E402


# --- text cleaning --------------------------------------------------------


def test_clean_rejoins_hard_wrapped_lines():
    """PDF text layers wrap mid-sentence; the model should not see the breaks."""
    raw = "The treatment is given to\nparticipants over several\nmonths."
    assert ingest._clean(raw) == "The treatment is given to participants over several months."


def test_clean_keeps_real_line_breaks():
    """A break after terminal punctuation is a genuine boundary, so keep it."""
    raw = "First point.\nSecond point."
    assert "\n" in ingest._clean(raw)


def test_clean_collapses_blank_runs_and_spaces():
    assert ingest._clean("A   B\n\n\n\nC") == "A B\n\nC"


# --- PDF extraction -------------------------------------------------------


def _make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def test_extract_pages_reads_every_page():
    pdf = _make_pdf(["Alpha slide", "Beta slide", "Gamma slide"])
    pages = ingest.extract_pages(pdf, want_images=False)

    assert len(pages) == 3
    assert "Alpha" in pages[0][0]
    assert "Gamma" in pages[2][0]
    assert all(image is None for _, image in pages), "no images requested"


def test_extract_pages_renders_images_when_asked():
    pdf = _make_pdf(["Only slide"])
    pages = ingest.extract_pages(pdf, want_images=True)

    image = pages[0][1]
    assert image is not None
    assert image[:8] == b"\x89PNG\r\n\x1a\n", "expected a PNG"


def test_extract_pages_caps_at_max_slides(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_SLIDES", 2)
    pdf = _make_pdf(["one", "two", "three", "four"])

    assert len(ingest.extract_pages(pdf, want_images=False)) == 2


def test_extract_pages_rejects_non_pdf():
    with pytest.raises(ingest.IngestError):
        ingest.extract_pages(b"this is not a pdf", want_images=False)


# --- narration response handling ------------------------------------------


def test_strip_fence_unwraps_json_code_block():
    fenced = '```json\n{"title": "T"}\n```'
    assert narration._strip_fence(fenced) == '{"title": "T"}'


def test_coerce_caps_bullets_and_keywords():
    result = narration._coerce(
        {
            "title": "A title",
            "bullets": [f"b{i}" for i in range(9)],
            "narration": "Something spoken.",
            "keywords": [f"k{i}" for i in range(20)],
        },
        1,
    )
    assert len(result["bullets"]) == 4
    assert len(result["keywords"]) == 12


def test_coerce_rejects_empty_narration():
    with pytest.raises(ValueError):
        narration._coerce({"title": "T", "bullets": [], "narration": "  "}, 1)


def test_coerce_falls_back_to_slide_number_for_missing_title():
    result = narration._coerce({"narration": "Spoken."}, 7)
    assert result["title"] == "Slide 7"


def test_placeholder_is_presentable():
    """A failed slide still has to be something the agent can say out loud."""
    slide = ingest._placeholder(3, "Some extracted text about revenue growth")

    assert slide.id == 3
    assert slide.bullets, "a slide with no bullets would render empty"
    assert slide.narration.strip()


# --- storage --------------------------------------------------------------


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DECKS_DIR", tmp_path / "decks")
    return tmp_path


async def test_save_and_load_round_trip(temp_store):
    deck = Deck(
        id="abc123",
        title="A Deck",
        slides=[Slide(id=1, title="One", bullets=["a", "b"], narration="Spoken.", keywords=["x"])],
        source="a.pdf",
    )
    await store.save(deck)

    loaded = store.get("abc123")
    assert loaded is not None
    assert loaded.title == "A Deck"
    assert loaded.slides[0].narration == "Spoken."
    assert loaded.slides[0].keywords == ["x"]


async def test_builtin_deck_comes_from_code_not_disk(temp_store):
    """A fresh clone must always have something to present."""
    deck = store.get("clinical-trial-phases")
    assert deck is not None
    assert deck.slide_count == 6
    assert deck.builtin


def test_builtin_deck_cannot_be_deleted(temp_store):
    assert store.delete("clinical-trial-phases") is False


async def test_list_all_puts_builtin_last(temp_store):
    await store.save(Deck(id="aaa111", title="Uploaded", status="ready"))

    decks = store.list_all()
    assert decks[-1].builtin
    assert any(d.id == "aaa111" for d in decks)


def test_path_traversal_ids_are_rejected(temp_store):
    assert store.get("../../etc/passwd") is None
    assert store.delete("../../etc/passwd") is False


async def test_unreadable_deck_file_is_skipped_not_fatal(temp_store):
    (temp_store / "decks").mkdir(parents=True, exist_ok=True)
    (temp_store / "decks" / "broken.json").write_text("{not json", encoding="utf-8")

    decks = store.list_all()  # must not raise
    assert any(d.builtin for d in decks)


# --- deck model -----------------------------------------------------------


def test_public_slides_never_leak_narration():
    """Narration reaches the model as a tool result, never the browser."""
    for slide in builtin_deck().public_slides():
        assert "narration" not in slide
        assert set(slide) == {"id", "title", "bullets"}


def test_tool_result_carries_narration():
    result = builtin_deck().get_slide(5).to_tool_result()
    assert "one thousand to three thousand" in result["content"]


# --- slide images ---------------------------------------------------------


def test_extract_pages_writes_images_to_disk(temp_store, monkeypatch):
    """Rendering is what makes the viewer show the real slide, not a rebuild."""
    monkeypatch.setattr(store, "DECKS_DIR", temp_store / "decks")
    pdf = _make_pdf(["one", "two"])

    ingest.extract_pages(pdf, want_images=True, deck_id="deck123")

    for n in (1, 2):
        path = store.image_path("deck123", n)
        assert path.is_file(), f"slide {n} image missing"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_extract_pages_skips_disk_when_no_deck_id(temp_store, monkeypatch):
    monkeypatch.setattr(store, "DECKS_DIR", temp_store / "decks")
    ingest.extract_pages(_make_pdf(["one"]), want_images=True)

    assert not store.image_dir("deck123").exists()


async def test_delete_removes_rendered_images(temp_store, monkeypatch):
    """Deleting a deck must not leave its PNGs orphaned on disk."""
    monkeypatch.setattr(store, "DECKS_DIR", temp_store / "decks")

    deck = Deck(id="deck456", title="With images", status="ready", has_images=True)
    await store.save(deck)
    ingest.extract_pages(_make_pdf(["a", "b"]), want_images=True, deck_id="deck456")
    assert store.image_dir("deck456").is_dir()

    assert store.delete("deck456") is True
    assert not store.image_dir("deck456").exists()


async def test_has_images_survives_a_storage_round_trip(temp_store, monkeypatch):
    monkeypatch.setattr(store, "DECKS_DIR", temp_store / "decks")
    await store.save(Deck(id="deck789", title="T", status="ready", has_images=True))

    assert store.get("deck789").has_images is True


def test_builtin_deck_has_no_images():
    """It has no source PDF, so the viewer must fall back to rendering bullets."""
    assert builtin_deck().has_images is False


# --- artifact-opener self-correction --------------------------------------


async def test_artifact_opener_triggers_one_corrective_retry():
    """Narration that describes the slide as an object must be rewritten."""
    calls: list[str | None] = []

    async def attempt(extra):
        calls.append(extra)
        if extra is None:
            return {"title": "T", "bullets": [], "narration": "This slide shows revenue.",
                    "keywords": []}
        return {"title": "T", "bullets": [], "narration": "Revenue grew forty percent.",
                "keywords": []}

    result = await narration._with_artifact_correction(attempt)

    assert len(calls) == 2, "expected exactly one retry"
    assert calls[1] is not None, "retry must carry the corrective instruction"
    assert result["narration"] == "Revenue grew forty percent."


async def test_clean_narration_is_not_retried():
    """A good first pass must not cost a second API call."""
    calls = []

    async def attempt(extra):
        calls.append(extra)
        return {"title": "T", "bullets": [], "narration": "Revenue grew.", "keywords": []}

    await narration._with_artifact_correction(attempt)
    assert len(calls) == 1


async def test_failed_retry_keeps_the_first_result():
    """One flawed narration beats a slide with none at all."""

    async def attempt(extra):
        if extra is None:
            return {"title": "T", "bullets": [], "narration": "The slide lists three goals.",
                    "keywords": []}
        raise RuntimeError("model unavailable")

    result = await narration._with_artifact_correction(attempt)
    assert result["narration"] == "The slide lists three goals."


def test_artifact_opener_matches_real_failure_modes():
    for bad in ("This slide shows...", "The slide lists two things", "this page contains",
                "The deck covers"):
        assert narration._ARTIFACT_OPENER.match(bad), f"should flag: {bad!r}"
    for good in ("Revenue grew forty percent.", "Thistle is a plant.",
                 "These are the three phases."):
        assert not narration._ARTIFACT_OPENER.match(good), f"should allow: {good!r}"


# --- deck synthesis -------------------------------------------------------


def test_apply_synthesis_sets_transitions_by_slide_number():
    deck = builtin_deck()
    deck.apply_synthesis(
        {"intro": "Welcome.", "outro": "Thanks.", "persona": "A researcher.",
         "transitions": {2: "Next up.", 4: "Now the bigger study."}}
    )

    assert deck.intro == "Welcome."
    assert deck.persona == "A researcher."
    assert deck.get_slide(2).transition == "Next up."
    assert deck.get_slide(4).transition == "Now the bigger study."
    # Slide 1 has no predecessor, and slide 3 got no transition in this payload.
    assert deck.get_slide(1).transition == ""
    assert deck.get_slide(3).transition == ""


def test_synthesis_survives_a_storage_round_trip(tmp_path):
    deck = builtin_deck()
    deck.apply_synthesis(
        {"intro": "Hello.", "outro": "Bye.", "persona": "A teacher.", "transitions": {2: "Then."}}
    )

    restored = Deck.from_storage(deck.to_storage())

    assert restored.intro == "Hello."
    assert restored.outro == "Bye."
    assert restored.persona == "A teacher."
    assert restored.get_slide(2).transition == "Then."


def test_has_synthesis_reflects_whether_the_pass_ran():
    deck = builtin_deck()
    assert deck.summary()["has_synthesis"] is False

    deck.apply_synthesis({"intro": "Welcome.", "outro": "", "persona": "", "transitions": {}})
    assert deck.summary()["has_synthesis"] is True


def test_prompt_embeds_persona_intro_outro_and_transitions():
    """The synthesis output has to actually reach the presenter's instructions."""
    import prompts

    deck = builtin_deck()
    deck.apply_synthesis(
        {"intro": "Welcome to the four stages.", "outro": "Safety gates every step.",
         "persona": "A calm clinical researcher.", "transitions": {2: "Before any human dose."}}
    )
    text = prompts.build_system_prompt(deck)

    assert "A calm clinical researcher." in text
    assert "Welcome to the four stages." in text
    assert "Safety gates every step." in text
    assert "Before any human dose." in text


def test_prompt_without_synthesis_still_builds():
    """A deck whose synthesis failed must still be presentable."""
    import prompts

    text = prompts.build_system_prompt(builtin_deck())

    assert "<persona>" not in text
    assert "Compose a 2-3 sentence opening yourself" in text
