"""Tests for presenter selection.

The registry itself is data, so what is worth testing is the behaviour around
it: that a bad id degrades instead of killing a session, that every presenter
really is distinct (a picker whose options sound identical is worse than no
picker), and that the delivery style actually reaches the system prompt
without displacing the deck's own persona.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import presenters  # noqa: E402
from presenters import PRESENTERS, get, voice_id_for  # noqa: E402
from prompts import build_system_prompt  # noqa: E402
from slides import Deck, Slide  # noqa: E402


# --- the registry ----------------------------------------------------------


def test_every_presenter_has_a_distinct_voice():
    """Two presenters sharing a voice would make the choice meaningless."""
    voices = [p.voice_id for p in PRESENTERS]
    assert len(set(voices)) == len(voices)


def test_every_presenter_has_a_distinct_id():
    ids = [p.id for p in PRESENTERS]
    assert len(set(ids)) == len(ids)


def test_every_presenter_is_fully_described():
    """Each one appears on a card; a blank field would render as a gap."""
    for p in PRESENTERS:
        assert p.name and p.tagline and p.blurb and p.accent and p.category
        assert p.voice_id and len(p.voice_id) > 20
        assert p.style.strip()
        assert len(p.tone) >= 2
        # Both stops must be real hex, or the orb renders as a grey disc.
        assert len(p.colors) == 2
        for stop in p.colors:
            assert stop.startswith("#") and len(stop) == 7


def test_every_presenter_has_a_distinct_colour():
    """The orb is the fastest way to tell cards apart; duplicates defeat it."""
    palettes = [p.colors for p in PRESENTERS]
    assert len(set(palettes)) == len(palettes)


def test_icons_are_known_to_the_frontend():
    """A glyph name with no matching path would silently draw nothing."""
    # Mirrors the ICONS map in PresenterPicker.tsx.
    known = {"chart", "spark", "book", "brief", "quote", "compass"}
    for p in PRESENTERS:
        assert p.icon in known, f"{p.id} uses unknown icon {p.icon!r}"


def test_public_payload_carries_what_the_card_needs():
    payload = PRESENTERS[0].public()
    assert set(payload) == {
        "id", "name", "tagline", "blurb", "accent", "tone",
        "category", "icon", "colors",
    }
    # The delivery style is prompt engineering, not user-facing copy.
    assert "style" not in payload
    # Nor is the voice id needed to draw a card.
    assert len(payload["colors"]) == 2


# --- resolution ------------------------------------------------------------


def test_known_id_resolves():
    assert get("analyst").id == "analyst"


def test_id_is_case_and_space_insensitive():
    assert get("  ANALYST  ").id == "analyst"


@pytest.mark.parametrize("bad", [None, "", "   ", "nope", "does-not-exist"])
def test_unknown_id_falls_back_rather_than_raising(bad):
    """A stale link must not cost someone their session."""
    assert get(bad) in PRESENTERS


def test_default_presenter_is_one_that_exists():
    assert presenters.DEFAULT_PRESENTER_ID in presenters.BY_ID


# --- voice selection -------------------------------------------------------


def test_presenter_voice_wins_over_the_env_override(monkeypatch):
    """The whole feature dies if CARTESIA_VOICE_ID silences the choice.

    That variable is set in normal development, so if it took priority every
    presenter would sound the same and the picker would be decoration.
    """
    monkeypatch.setenv("CARTESIA_VOICE_ID", "some-pinned-voice")
    for p in PRESENTERS:
        assert voice_id_for(p) == p.voice_id


def test_env_voice_is_used_only_when_a_presenter_has_none(monkeypatch):
    monkeypatch.setenv("CARTESIA_VOICE_ID", "fallback-voice")
    voiceless = presenters.Presenter(
        id="x", name="X", tagline="t", blurb="b", voice_id="",
        style="s", accent="a", tone=("one", "two"),
        category="c", icon="spark", colors=("#000000", "#ffffff"),
    )
    assert voice_id_for(voiceless) == "fallback-voice"


# --- the prompt ------------------------------------------------------------


def _deck(persona: str = "") -> Deck:
    return Deck(
        id="d",
        title="A Deck",
        slides=[Slide(id=1, title="One", bullets=["a"], narration="Words.", keywords=["k"])],
        persona=persona,
    )


def test_style_reaches_the_prompt():
    prompt = build_system_prompt(_deck(), presenters.get("briefer").style)
    assert "executive briefing" in prompt.lower()
    assert "<persona>" in prompt


def test_deck_persona_is_listed_before_delivery_style():
    """Subject-matter register should win where the two disagree."""
    prompt = build_system_prompt(_deck("A trial statistician."), "Speak quickly.")
    assert prompt.index("A trial statistician.") < prompt.index("Speak quickly.")
    assert "Delivery:" in prompt


def test_style_alone_still_produces_a_persona_block():
    prompt = build_system_prompt(_deck(), "Speak plainly.")
    assert "<persona>" in prompt
    assert "Speak plainly." in prompt


def test_no_persona_and_no_style_means_no_block():
    prompt = build_system_prompt(_deck(), "")
    assert "<persona>" not in prompt


def test_every_presenter_produces_a_usable_prompt():
    """A malformed style would break every session using that presenter."""
    for p in PRESENTERS:
        prompt = build_system_prompt(_deck("Someone."), p.style)
        assert "<persona>" in prompt and "</persona>" in prompt
        assert "<deck>" in prompt
