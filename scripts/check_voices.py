"""Verify every presenter voice still resolves in Cartesia's catalogue.

    python scripts/check_voices.py

A voice id that has been retired upstream does not fail at startup. It fails
when someone picks that presenter and the session goes silent, which is the
least convenient moment to find out. Run this after changing the registry, and
periodically against production.

Exits non-zero if any voice is missing, so it can gate a deploy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
load_dotenv(ROOT / ".env", override=True)

from presenters import PRESENTERS  # noqa: E402

CATALOGUE_URL = "https://api.cartesia.ai/voices/"


def main() -> int:
    api_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if not api_key:
        print("CARTESIA_API_KEY is not set.")
        return 2

    try:
        response = httpx.get(
            CATALOGUE_URL,
            headers={"X-API-Key": api_key, "Cartesia-Version": "2024-06-10"},
            params={"limit": 100},
            timeout=60,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"Could not read the Cartesia voice catalogue: {exc}")
        return 2

    catalogue = {voice["id"]: voice for voice in response.json()}
    print(f"{len(catalogue)} voices in the catalogue\n")

    missing = []
    for presenter in PRESENTERS:
        voice = catalogue.get(presenter.voice_id)
        if voice:
            print(f"  ok      {presenter.name:8} {presenter.tagline:16} {voice['name']}")
        else:
            missing.append(presenter)
            print(f"  MISSING {presenter.name:8} {presenter.tagline:16} {presenter.voice_id}")

    # Two presenters sharing a voice makes the picker meaningless, so it is
    # worth catching here as well as in the test suite.
    voices = [p.voice_id for p in PRESENTERS]
    duplicates = len(voices) - len(set(voices))

    print()
    if missing:
        print(f"{len(missing)} presenter(s) reference a voice that no longer exists.")
    if duplicates:
        print(f"{duplicates} presenter(s) share a voice with another presenter.")
    if not missing and not duplicates:
        print(f"All {len(PRESENTERS)} presenters resolve to distinct, current voices.")

    return 1 if (missing or duplicates) else 0


if __name__ == "__main__":
    raise SystemExit(main())
