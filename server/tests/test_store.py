"""Tests for deleting a deck that something is still writing to.

Ingestion runs in the background and saves its deck after every step, so a
delete has to stick even when a save comes after it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402
from slides import Deck  # noqa: E402


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DECKS_DIR", tmp_path / "decks")
    monkeypatch.setattr(store, "_deleted", set())
    return tmp_path / "decks"


def _write_images(deck_id: str, count: int) -> None:
    store.image_dir(deck_id).mkdir(parents=True, exist_ok=True)
    for n in range(1, count + 1):
        store.image_path(deck_id, n).write_bytes(b"\x89PNG")


async def test_a_save_after_delete_does_not_bring_the_deck_back(temp_store):
    deck = Deck(id="del1", title="Parsing", status="processing")
    await store.save(deck)
    assert store.delete("del1") is True

    deck.progress = 3
    with pytest.raises(store.DeckDeleted):
        await store.save(deck)

    assert store.get("del1") is None
    assert not (temp_store / "del1.json").exists()
    assert all(d.id != "del1" for d in store.list_all())


async def test_delete_is_recorded(temp_store):
    await store.save(Deck(id="del2", title="T", status="ready"))
    assert store.was_deleted("del2") is False

    store.delete("del2")

    assert store.was_deleted("del2") is True


async def test_a_refused_delete_is_not_recorded(temp_store):
    """Only a deck that was really deleted stops its saves."""
    assert store.delete("nosuchdeck") is False
    assert store.delete("clinical-trial-phases") is False

    assert store.was_deleted("nosuchdeck") is False
    await store.save(Deck(id="nosuchdeck", title="T", status="ready"))
    assert store.get("nosuchdeck") is not None


async def test_other_decks_still_save_after_a_delete(temp_store):
    await store.save(Deck(id="del3", title="Gone", status="ready"))
    store.delete("del3")

    await store.save(Deck(id="keep1", title="Kept", status="ready"))

    assert store.get("keep1") is not None


async def test_delete_succeeds_while_a_page_is_still_being_written(temp_store):
    """A file the delete didn't expect in the image directory must not fail it."""
    await store.save(Deck(id="del4", title="T", status="processing"))
    _write_images("del4", 2)
    # Stands in for a page the renderer writes between the glob and the rmdir.
    (store.image_dir("del4") / "slide-003.png.partial").write_bytes(b"")

    assert store.delete("del4") is True

    assert store.get("del4") is None
    assert not list(store.image_dir("del4").glob("slide-*.png"))


def test_remove_images_takes_the_directory(temp_store):
    _write_images("img1", 3)

    store.remove_images("img1")

    assert not store.image_dir("img1").exists()


def test_remove_images_without_a_directory_is_a_no_op(temp_store):
    store.remove_images("img2")  # must not raise
