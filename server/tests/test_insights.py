"""Tests for post-session transcript analysis.

The model call is stubbed throughout - what matters here is everything around
it: that counted facts are counted rather than asked for, that a malformed or
hostile model reply cannot reach the frontend, and that empty sessions are
refused with a reason instead of producing a confident analysis of nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import insights  # noqa: E402
from insights import InsightsError, _clean_turns, _coerce, analyze, count_facts  # noqa: E402


# --- turn cleaning ---------------------------------------------------------


def test_empty_and_non_spoken_turns_are_dropped():
    """The browser sends empty assistant turns the moment a reply starts."""
    turns = [
        {"role": "user", "text": "What is phase two?"},
        {"role": "assistant", "text": ""},
        {"role": "system", "text": "ignore me"},
        {"role": "assistant", "text": "Phase two tests efficacy."},
        {"role": "user", "text": "   "},
    ]
    assert _clean_turns(turns) == [
        {"role": "user", "text": "What is phase two?"},
        {"role": "assistant", "text": "Phase two tests efficacy."},
    ]


def test_whitespace_is_normalised():
    got = _clean_turns([{"role": "user", "text": "  what\n\n about   this?  "}])
    assert got == [{"role": "user", "text": "what about this?"}]


# --- counted facts ---------------------------------------------------------


def test_counts_are_exact():
    turns = [
        {"role": "user", "text": "one two three"},
        {"role": "assistant", "text": "four five"},
        {"role": "user", "text": "six"},
    ]
    assert count_facts(turns) == {
        "questions_asked": 2,
        "agent_replies": 1,
        "user_words": 4,
        "agent_words": 2,
        "total_turns": 3,
    }


# --- coercion of the model's reply -----------------------------------------


def test_unknown_intent_becomes_unclassified_not_invented():
    out = _coerce({"exchanges": [{"question": "q", "intent": "vibes", "quality": 3}]})
    assert out["exchanges"][0]["intent"] == "unclassified"


def test_quality_is_clamped_to_the_documented_scale():
    out = _coerce(
        {
            "exchanges": [
                {"question": "a", "intent": "factual_lookup", "quality": 99},
                {"question": "b", "intent": "factual_lookup", "quality": -4},
                {"question": "c", "intent": "factual_lookup", "quality": "not a number"},
            ]
        }
    )
    assert [e["quality"] for e in out["exchanges"]] == [5, 0, 0]


def test_scores_are_derived_from_the_visible_exchanges():
    """The headline average must agree with the per-row scores a reader sees."""
    out = _coerce(
        {
            "exchanges": [
                {"question": "a", "intent": "factual_lookup", "quality": 4, "answered": True,
                 "grounded": True},
                {"question": "b", "intent": "off_topic", "quality": 5, "answered": True,
                 "grounded": True},
                {"question": "c", "intent": "challenge", "quality": 3, "answered": False,
                 "grounded": True},
            ]
        }
    )
    assert out["scores"]["average_quality"] == 4.0
    assert out["scores"]["answered"] == 2
    assert out["scores"]["grounded"] == 3
    assert out["scores"]["evaluated"] == 3


def test_average_is_none_rather_than_zero_when_nothing_scored():
    """Zero would read as "terrible session"; None reads as "not scored"."""
    out = _coerce({"exchanges": []})
    assert out["scores"]["average_quality"] is None


def test_missing_fields_become_empty_not_errors():
    out = _coerce({})
    assert out["summary"] == ""
    assert out["exchanges"] == []
    assert out["strengths"] == []
    assert out["recommendations"] == []


def test_null_fields_become_empty_not_the_word_none():
    """A JSON null is a missing value; str(None) would put "None" on screen."""
    out = _coerce(
        {
            "summary": None,
            "audience_intent": None,
            "exchanges": [
                {
                    "question": None,
                    "intent": None,
                    "intent_note": None,
                    "quality_note": None,
                }
            ],
            "strengths": [None, "stayed grounded"],
            "deck_gaps": [None],
        }
    )
    assert out["summary"] == ""
    assert out["audience_intent"] == ""
    ex = out["exchanges"][0]
    assert ex["question"] == ""
    assert ex["intent"] == "unclassified"
    assert ex["intent_note"] == ""
    assert ex["quality_note"] == ""
    assert out["strengths"] == ["stayed grounded"]
    assert out["deck_gaps"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        (" True ", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("no", False),
        ("", False),
        (1, True),
        (0, False),
        (None, False),
        ([], False),
    ],
)
def test_answered_and_grounded_read_quoted_booleans_as_written(value, expected):
    """bool("false") is True, which would count a dodge as answered."""
    out = _coerce({"exchanges": [{"answered": value, "grounded": value, "quality": 3}]})
    ex = out["exchanges"][0]
    assert ex["answered"] is expected
    assert ex["grounded"] is expected
    assert out["scores"]["answered"] == int(expected)
    assert out["scores"]["grounded"] == int(expected)


def test_non_object_reply_is_rejected():
    with pytest.raises(ValueError):
        _coerce(["not", "an", "object"])


def test_garbage_list_items_are_skipped():
    out = _coerce({"exchanges": ["a string", 42, {"question": "real", "intent": "social"}]})
    assert len(out["exchanges"]) == 1
    assert out["exchanges"][0]["question"] == "real"


def test_list_fields_are_capped():
    out = _coerce({"strengths": [f"item {i}" for i in range(50)]})
    assert len(out["strengths"]) == 6


# --- analyze() -------------------------------------------------------------


async def test_refuses_an_empty_transcript():
    with pytest.raises(InsightsError, match="no conversation"):
        await analyze(api_key="k", deck_title="D", turns=[])


async def test_refuses_a_session_where_nobody_asked_anything():
    """A monologue has nothing to evaluate; say so rather than inventing."""
    turns = [{"role": "assistant", "text": "Welcome to the deck."}]
    with pytest.raises(InsightsError, match="Nobody asked"):
        await analyze(api_key="k", deck_title="D", turns=turns)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the request so the prompt can be asserted on."""

    def __init__(self, payload, sink):
        self._payload = payload
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._sink.append(json)
        return _FakeResponse(self._payload)


def _reply(content: str, finish_reason: str = "stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


async def test_happy_path_returns_counts_and_analysis(monkeypatch):
    sent: list[dict] = []
    body = _reply(
        '{"summary": "Went well.", "audience_intent": "Understand phases.",'
        ' "exchanges": [{"question": "What is phase two?", "intent": "factual_lookup",'
        ' "answered": true, "grounded": true, "quality": 5, "quality_note": "Direct."}],'
        ' "strengths": ["stayed grounded"], "weaknesses": [], "deck_gaps": [],'
        ' "recommendations": ["add a summary slide"]}'
    )
    monkeypatch.setattr(
        insights, "net_async_client", lambda **kw: _FakeClient(body, sent)
    )

    result = await analyze(
        api_key="k",
        deck_title="Clinical Trials",
        turns=[
            {"role": "user", "text": "What is phase two?"},
            {"role": "assistant", "text": "Phase two tests whether it works."},
        ],
        slide_titles=["Intro", "Phase II"],
    )

    assert result["summary"] == "Went well."
    assert result["scores"]["average_quality"] == 5.0
    # Counted locally, never taken from the model.
    assert result["counts"]["questions_asked"] == 1
    assert result["deck_title"] == "Clinical Trials"

    prompt = sent[0]["messages"][1]["content"]
    # The outline is what lets the model judge a refusal as correct.
    assert "Phase II" in prompt
    assert "AUDIENCE: What is phase two?" in prompt


async def test_counts_reflect_the_whole_session_even_when_the_prompt_is_trimmed(
    monkeypatch,
):
    """Truncation is a prompt-size measure; the reported counts must not lie."""
    monkeypatch.setattr(insights, "MAX_TURNS", 4)
    sent: list[dict] = []
    monkeypatch.setattr(
        insights, "net_async_client", lambda **kw: _FakeClient(_reply("{}"), sent)
    )

    turns = []
    for i in range(20):
        turns.append({"role": "user", "text": f"question {i}"})
        turns.append({"role": "assistant", "text": f"answer {i}"})

    result = await analyze(api_key="k", deck_title="D", turns=turns)

    assert result["counts"]["questions_asked"] == 20
    prompt = sent[0]["messages"][1]["content"]
    assert "question 19" in prompt
    assert "question 0" not in prompt


async def test_truncated_reply_is_reported_not_parsed(monkeypatch):
    monkeypatch.setattr(
        insights,
        "net_async_client",
        lambda **kw: _FakeClient(_reply('{"summary": "cut o', "length"), []),
    )
    with pytest.raises(InsightsError, match="cut off"):
        await analyze(
            api_key="k",
            deck_title="D",
            turns=[{"role": "user", "text": "hi"}],
        )


async def test_non_json_reply_is_reported(monkeypatch):
    monkeypatch.setattr(
        insights,
        "net_async_client",
        lambda **kw: _FakeClient(_reply("I'm afraid I can't do that"), []),
    )
    with pytest.raises(InsightsError, match="unusable"):
        await analyze(api_key="k", deck_title="D", turns=[{"role": "user", "text": "hi"}])


async def test_fenced_json_is_still_parsed(monkeypatch):
    """Models fence JSON despite being told not to."""
    monkeypatch.setattr(
        insights,
        "net_async_client",
        lambda **kw: _FakeClient(_reply('```json\n{"summary": "Fine."}\n```'), []),
    )
    result = await analyze(
        api_key="k", deck_title="D", turns=[{"role": "user", "text": "hi"}]
    )
    assert result["summary"] == "Fine."


async def test_network_failure_becomes_an_insights_error(monkeypatch):
    class Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(insights, "net_async_client", lambda **kw: Boom())
    with pytest.raises(InsightsError, match="Could not reach"):
        await analyze(api_key="k", deck_title="D", turns=[{"role": "user", "text": "hi"}])
