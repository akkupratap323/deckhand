"""Deck storage.

File-backed JSON, one file per deck, under ``DATA_DIR``. Not a database on
purpose: decks are small, writes are rare, and a directory of readable JSON is
easy to inspect and to mount as a Docker volume. Swapping this for Postgres
would touch this file and nothing else.

The built-in deck is never written to disk - it is returned from code so a fresh
clone always has something to present.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from loguru import logger

from slides import DEFAULT_DECK_ID, Deck, builtin_deck

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent / "data"))
DECKS_DIR = DATA_DIR / "decks"

# Serializes read-modify-write on the same deck during ingestion.
_lock = asyncio.Lock()


def _path(deck_id: str) -> Path:
    return DECKS_DIR / f"{deck_id}.json"


def image_dir(deck_id: str) -> Path:
    """Directory holding one rendered PNG per slide."""
    return DECKS_DIR / deck_id


def image_path(deck_id: str, slide_number: int) -> Path:
    """Where slide ``slide_number``'s rendered page image lives."""
    return image_dir(deck_id) / f"slide-{slide_number:03d}.png"


def _safe_id(deck_id: str) -> bool:
    """Guard against a deck id escaping DECKS_DIR via path traversal."""
    return bool(deck_id) and deck_id.replace("-", "").replace("_", "").isalnum()


def init() -> None:
    """Create the storage directory. Called once at startup."""
    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Deck storage at {DECKS_DIR}")


async def save(deck: Deck) -> None:
    """Write a deck, atomically enough that a crash cannot leave a half file."""
    if not _safe_id(deck.id):
        raise ValueError(f"Unsafe deck id: {deck.id!r}")

    async with _lock:
        DECKS_DIR.mkdir(parents=True, exist_ok=True)
        target = _path(deck.id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(deck.to_storage(), indent=2), encoding="utf-8")
        tmp.replace(target)


def get(deck_id: str) -> Deck | None:
    """Load one deck. The built-in deck comes from code, not disk."""
    if deck_id == DEFAULT_DECK_ID:
        return builtin_deck()
    if not _safe_id(deck_id):
        return None

    path = _path(deck_id)
    if not path.is_file():
        return None
    try:
        return Deck.from_storage(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning(f"Skipping unreadable deck file {path.name}: {exc}")
        return None


def list_all() -> list[Deck]:
    """Every deck, newest first, with the built-in one pinned last."""
    decks: list[Deck] = []
    if DECKS_DIR.is_dir():
        for path in DECKS_DIR.glob("*.json"):
            try:
                decks.append(Deck.from_storage(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning(f"Skipping unreadable deck file {path.name}: {exc}")

    decks.sort(key=lambda d: d.created_at, reverse=True)
    decks.append(builtin_deck())
    return decks


def delete(deck_id: str) -> bool:
    """Remove an uploaded deck and its rendered images.

    The built-in deck cannot be deleted.
    """
    if deck_id == DEFAULT_DECK_ID or not _safe_id(deck_id):
        return False
    path = _path(deck_id)
    if not path.is_file():
        return False

    path.unlink()

    # Slide images live in a sibling directory; take them with it.
    images = image_dir(deck_id)
    if images.is_dir():
        for png in images.glob("slide-*.png"):
            png.unlink()
        images.rmdir()

    logger.info(f"Deleted deck {deck_id}")
    return True
