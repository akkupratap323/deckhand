"""Post-session analysis of a presentation conversation.

After a session ends, the transcript is worth more than a log: it says which
questions the audience actually asked, whether the agent answered them from
the deck or dodged, and where the deck left people wanting. This turns a
transcript into that assessment.

Two kinds of number come out of here, and they are deliberately kept apart:

*Counted* facts - how many questions were asked, how many the agent refused as
off-deck, which slides were visited - are computed in Python from the
transcript and the session's own record. They are exact, and no model is asked
to produce them, because a model asked to count will occasionally be wrong and
there is no reason to accept that.

*Judged* assessments - what a question was really asking, whether the answer
was actually responsive, how grounded it was in the deck - need a model, and
are returned with the model's own reasoning attached so a reader can disagree
with them. Every score is on a stated 1-5 scale with the rubric in the prompt,
so "4" means the same thing across runs.

The whole thing is best-effort: a failure here loses an after-the-fact report,
not any part of the presentation, so callers get a clear error rather than
having a session's end blocked on it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from net import async_client as net_async_client

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# Generous, and for the same reason narration's budget is: DeepSeek's v4 models
# spend completion tokens on hidden reasoning before emitting any visible
# output, so a budget sized to the answer alone truncates the answer.
INSIGHTS_MAX_TOKENS = int(os.environ.get("INSIGHTS_MAX_TOKENS", "4000"))

# Beyond this the prompt gets unwieldy and the analysis loses focus. Long
# sessions are truncated from the *start*, keeping the most recent exchanges.
MAX_TURNS = int(os.environ.get("INSIGHTS_MAX_TURNS", "80"))

SYSTEM_PROMPT = """\
You evaluate a recorded conversation between an audience and an AI agent that \
presents a slide deck aloud.

The agent is supposed to: present the deck's content, answer questions using \
only what is in the deck, navigate to the slide that answers a question, and \
politely refuse questions the deck does not cover rather than answering them \
from general knowledge. Refusing an off-deck question is CORRECT behaviour and \
must be scored as such - it is grounding working, not a failure.

Return ONLY a JSON object, no prose and no code fence, in exactly this shape:

{
  "summary": "2-3 sentences on how the session went overall.",
  "audience_intent": "1-2 sentences on what the audience was collectively trying to learn.",
  "exchanges": [
    {
      "question": "the audience's question, quoted or lightly cleaned up",
      "intent": "one of: factual_lookup, clarification, deeper_detail, \
challenge, off_topic, navigation, social",
      "intent_note": "one short sentence on what they were really after",
      "answered": true,
      "grounded": true,
      "quality": 4,
      "quality_note": "one sentence justifying the score"
    }
  ],
  "strengths": ["short phrases, 2-4 items"],
  "weaknesses": ["short phrases, 2-4 items"],
  "deck_gaps": ["topics the audience wanted that the deck does not cover, 0-4 items"],
  "recommendations": ["concrete, actionable, 2-4 items"]
}

Field rules:
- "intent" must be exactly one of the listed values.
- "answered": did the agent actually address the question that was asked?
  A correct, deliberate refusal of an off-deck question counts as answered.
- "grounded": was the reply consistent with deck material, rather than \
improvised from outside knowledge? A refusal is grounded.
- "quality": integer 1-5.
    5 = directly responsive, accurate, well-grounded, right level of detail
    4 = responsive and correct, slightly thin or slightly long
    3 = partially responsive, or correct but evasive
    2 = mostly unresponsive, or answered a different question
    1 = wrong, invented, or ignored the question
  Score a clean refusal of a genuinely off-deck question as 4 or 5.
- One entry in "exchanges" per audience question, in order. If the audience \
never asked anything, return an empty list.
- Use [] for any list with nothing to report. Never invent content that is not \
in the transcript.
"""

VALID_INTENTS = {
    "factual_lookup",
    "clarification",
    "deeper_detail",
    "challenge",
    "off_topic",
    "navigation",
    "social",
}


class InsightsError(Exception):
    """The analysis could not be produced."""


def _strip_fence(raw: str) -> str:
    """Models wrap JSON in ```json fences despite being told not to."""
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    return (m.group(1) if m else raw).strip()


def _clean_turns(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep only real spoken turns, in order, with usable text.

    The browser sends whatever its conversation state holds, which includes
    empty assistant turns created the moment a reply starts streaming, and can
    include roles the analysis has no use for.
    """
    cleaned: list[dict[str, str]] = []
    for turn in turns:
        role = str(turn.get("role", "")).lower()
        if role not in ("user", "assistant"):
            continue
        text = " ".join(str(turn.get("text", "")).split())
        if not text:
            continue
        cleaned.append({"role": role, "text": text})
    return cleaned


def count_facts(turns: list[dict[str, str]]) -> dict[str, int]:
    """Exact counts, computed rather than asked for. See the module docstring."""
    questions = sum(1 for t in turns if t["role"] == "user")
    replies = sum(1 for t in turns if t["role"] == "assistant")
    words_user = sum(len(t["text"].split()) for t in turns if t["role"] == "user")
    words_agent = sum(len(t["text"].split()) for t in turns if t["role"] == "assistant")
    return {
        "questions_asked": questions,
        "agent_replies": replies,
        "user_words": words_user,
        "agent_words": words_agent,
        "total_turns": len(turns),
    }


def _coerce(parsed: Any) -> dict[str, Any]:
    """Force the model's reply into the documented shape.

    Anything missing becomes an empty value rather than a KeyError downstream;
    anything out of range is clamped. The frontend renders whatever it gets, so
    this is the last place a malformed field can be caught.
    """
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")

    def text(value: Any) -> str:
        # A JSON null is a missing value, not the word "None" on screen.
        return "" if value is None else " ".join(str(value).split())

    def flag(value: Any) -> bool:
        # bool("false") is True, so a quoted "false" would count as answered.
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes")
        return bool(value)

    def strings(key: str, limit: int = 6) -> list[str]:
        raw = parsed.get(key)
        if not isinstance(raw, list):
            return []
        out = [t for t in (text(v) for v in raw) if t]
        return out[:limit]

    exchanges: list[dict[str, Any]] = []
    for item in parsed.get("exchanges") or []:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent", "")).strip().lower()
        if intent not in VALID_INTENTS:
            # Better an honest "unclassified" than a made-up category the UI
            # would have to guess how to display.
            intent = "unclassified"
        try:
            quality = int(item.get("quality", 0))
        except (TypeError, ValueError):
            quality = 0
        exchanges.append(
            {
                "question": text(item.get("question")),
                "intent": intent,
                "intent_note": text(item.get("intent_note")),
                "answered": flag(item.get("answered")),
                "grounded": flag(item.get("grounded")),
                "quality": max(0, min(5, quality)),
                "quality_note": text(item.get("quality_note")),
            }
        )

    scored = [e["quality"] for e in exchanges if e["quality"] > 0]
    return {
        "summary": text(parsed.get("summary")),
        "audience_intent": text(parsed.get("audience_intent")),
        "exchanges": exchanges,
        "strengths": strings("strengths"),
        "weaknesses": strings("weaknesses"),
        "deck_gaps": strings("deck_gaps"),
        "recommendations": strings("recommendations"),
        # Derived here, not asked of the model, so it always agrees with the
        # per-exchange scores the reader can see.
        "scores": {
            "average_quality": round(sum(scored) / len(scored), 2) if scored else None,
            "answered": sum(1 for e in exchanges if e["answered"]),
            "grounded": sum(1 for e in exchanges if e["grounded"]),
            "evaluated": len(exchanges),
        },
    }


def _transcript_text(turns: list[dict[str, str]]) -> str:
    lines = [f"{'AUDIENCE' if t['role'] == 'user' else 'AGENT'}: {t['text']}" for t in turns]
    return "\n".join(lines)


async def analyze(
    *,
    api_key: str,
    deck_title: str,
    turns: list[dict[str, Any]],
    slide_titles: list[str] | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Analyse one session's transcript.

    Args:
        api_key: DeepSeek key.
        deck_title: Title of the deck that was presented.
        turns: ``{"role": "user"|"assistant", "text": str}`` in spoken order.
        slide_titles: The deck's slide titles, so the model can judge whether a
            refusal was correct rather than guessing what the deck covered.
        model: Chat model to use.
        timeout: Per-request timeout.

    Returns:
        The analysis, plus exact ``counts`` computed locally.

    Raises:
        InsightsError: If there is nothing to analyse, or the model fails.
    """
    cleaned = _clean_turns(turns)
    if not cleaned:
        raise InsightsError("There is no conversation to analyse yet.")
    if not any(t["role"] == "user" for t in cleaned):
        raise InsightsError(
            "Nobody asked anything in this session, so there is nothing to evaluate."
        )

    counts = count_facts(cleaned)

    # Keep the most recent exchanges: the end of a long session is where the
    # interesting questions are, and the start is usually the scripted opening.
    trimmed = cleaned[-MAX_TURNS:]

    outline = ""
    if slide_titles:
        listed = "; ".join(f"{i + 1}. {t}" for i, t in enumerate(slide_titles))
        outline = f"\nThe deck's slides are: {listed}\n"

    user_content = (
        f'Deck presented: "{deck_title}"\n'
        f"{outline}"
        f"\nTranscript ({len(trimmed)} turns"
        f"{', truncated to the most recent' if len(cleaned) > MAX_TURNS else ''}):\n"
        f"---\n{_transcript_text(trimmed)}\n---"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": INSIGHTS_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    try:
        async with net_async_client(timeout=timeout) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        raise InsightsError(f"Could not reach the analysis model: {exc}") from exc

    choice = body["choices"][0]
    raw = choice["message"].get("content") or ""
    if choice.get("finish_reason") == "length":
        raise InsightsError(
            f"The analysis was cut off at {INSIGHTS_MAX_TOKENS} tokens. "
            "Try again with a shorter session."
        )

    try:
        parsed = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        raise InsightsError(f"The analysis model returned unusable output: {raw[:160]!r}") from exc

    try:
        result = _coerce(parsed)
    except ValueError as exc:
        raise InsightsError(str(exc)) from exc

    result["counts"] = counts
    result["deck_title"] = deck_title
    logger.info(
        f"Insights for {deck_title!r}: {counts['questions_asked']} question(s), "
        f"{len(result['exchanges'])} evaluated"
    )
    return result
