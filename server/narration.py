"""Turning a slide's rendered image and extracted text into something speakable.

A slide's raw text layer is not a script. It is fragments, orphaned headings,
page numbers, and bullet glyphs. This module asks an LLM to turn that into a
title, clean bullets, spoken narration, and routing keywords - and, when a
vision model is available, to read the rendered page image for the charts,
diagrams and layout the text layer misses entirely.

Several implementations behind one ``NarrationModel`` protocol, tried in this
order by ``build_narration_model()``:

1. ``GeminiVisionNarrationModel`` - Google's Generative Language API. This is
   the default vision path. Confirmed working: a real image call (a synthetic
   bar chart) round-trips correctly on ``gemini-3.6-flash``.
2. ``BedrockVisionNarrationModel`` - AWS Bedrock's Converse API, when
   ``BEDROCK_MODEL_ID`` and AWS credentials are set.
3. ``VisionNarrationModel`` - any other OpenAI-compatible vision endpoint via
   ``VISION_API_KEY`` / ``VISION_MODEL`` / ``VISION_BASE_URL``.
4. ``TextNarrationModel`` - the fallback every path above degrades to, and the
   only option if none of the above are configured. Needs nothing beyond the
   DeepSeek key the app already requires.

DeepSeek itself never appears as a vision option: its platform API
(``api.deepseek.com``) is text-only and rejects image content parts outright
with ``unknown variant 'image_url'``, on every model it exposes. It remains the
text model, and the fallback target for every vision implementation above.

The system prompt and the artifact-opener self-correction pattern (retry once,
with a corrective instruction, when narration starts by describing the slide as
an object rather than speaking its content) are adapted from the reference
implementation's Azure OpenAI adapter. See the README for the full comparison.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
from loguru import logger

from net import async_client as net_async_client

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# DeepSeek's v4 models are REASONING models: they spend completion tokens on
# hidden `reasoning_content` before emitting a single character of `content`.
# The budget therefore has to cover the thinking as well as the answer. Set it
# too low and the call does not error - it returns finish_reason="length" with
# content="" or with JSON truncated mid-object, which reads like a model bug
# until you look at usage.completion_tokens_details.reasoning_tokens.
NARRATION_MAX_TOKENS = int(os.environ.get("NARRATION_MAX_TOKENS", "3000"))
TITLE_MAX_TOKENS = int(os.environ.get("TITLE_MAX_TOKENS", "600"))
SYNTHESIS_MAX_TOKENS = int(os.environ.get("SYNTHESIS_MAX_TOKENS", "6000"))

SYSTEM_PROMPT = """You prepare one slide for an AI voice presenter that will deliver this deck
live, out loud, and answer audience questions about it afterward. What you write is the
presenter's ONLY knowledge of this slide - it cannot see the deck while presenting. Decks may
be corporate, technical, academic, or personal; accuracy matters more than style.

Return ONLY a JSON object, no markdown fence, with exactly these keys:

{
  "title": "short slide heading, at most 8 words - use the slide's own visible heading when
            there is one, otherwise a specific descriptive title, never a generic one",
  "bullets": ["3 to 4 short on-screen points, at most 10 words each"],
  "narration": "the presenter's spoken words about this slide - see NARRATION below",
  "keywords": ["6 to 12 terms a listener might use to ask about this slide"]
}

NARRATION - accuracy is non-negotiable:
- State only what the slide actually shows. Never invent numbers, names, or claims.
- Capture every specific: numbers with their units, percentages, dates, amounts, names,
  product or project names, versions. If an audience member asks "what was the exact
  figure?", the answer must be in the narration.
- Use the slide's own key terms verbatim at least once, so a spoken question about that
  topic can be matched back to this slide.
- If something is illegible or ambiguous, say what is visible rather than guess.

VISUALS - when the slide is more than plain text:
- Charts: what is plotted, the axes, the overall trend, and the standout values - peak,
  low, latest, biggest gap.
- Tables: the key rows and figures that carry the message, not a cell-by-cell dump.
- Diagrams and flows: walk through them in order - what connects to what, and what the
  direction means.
- Photos or decorative images: one brief mention, only if they carry meaning.

SPOKEN STYLE:
- Plain flowing sentences a person could read aloud. No markdown, bullet symbols, emojis,
  headers, or parenthetical asides.
- First sentence states the slide's main point; then supporting detail; then why it
  matters, if the slide implies it.
- HARD RULE - speak the content, never describe the artifact. Never write "This slide is
  titled...", "This slide shows...", or any opener that treats the slide as an object.
  WRONG: "This slide shows a chart about revenue growth."
  RIGHT: "Revenue grew forty percent to two point one million."
  WRONG: "This slide lists the team's two main goals."
  RIGHT: "The team is focused on two things this quarter: onboarding and retention."
- No greetings or handoffs to other slides - those are composed separately.

LENGTH: scale to the slide. One to two sentences for a title, agenda, or divider slide;
three to six for a typical content slide; up to eight for a dense chart, table, or
diagram. Never pad a sparse slide.

Write numbers as words where they would be spoken: "one thousand", not "1,000"."""

# Notes that open by describing the slide-as-object instead of speaking the
# content - the exact failure mode a first pass sometimes falls into.
_ARTIFACT_OPENER = re.compile(r"^\s*(this|the)\s+(slide|page|image|deck)\b", re.IGNORECASE)

_STYLE_CORRECTION = (
    "Your previous narration opened by describing the slide as an object "
    '("This slide shows..."). Rewrite the narration to state the content directly, as '
    "if speaking the material itself. Never refer to the slide, page, or image. Keep "
    "the same title, bullets, and keywords."
)

SYNTHESIS_SYSTEM_PROMPT = """You are preparing an AI voice presenter to deliver a slide deck
live. You are given the whole deck: every slide's title and narration, in order. Each
slide's narration is already written; your job is the connective tissue that makes it feel
like one coherent talk instead of separately-narrated pages.

Produce, as a single JSON object:

1. "intro": the presenter's opening words, spoken before slide one. 2 to 4 sentences.
   Welcome the audience, say in plain terms what this presentation is about, and preview
   the two to four main things it covers, in order. Do not just recite slide titles. If
   the deck title is a person's name or a filename rather than a real title, describe what
   the deck actually is instead of saying "welcome to <title>".

2. "transitions": for every slide from 2 to the last, one short spoken sentence bridging
   from the previous slide into this one - why the talk moves here next, or what question
   this slide answers. Max 20 words each. Vary the phrasing; never repeat the same opener
   twice in a row.

3. "outro": the presenter's closing words after the final slide. 1 to 3 sentences: the
   single main takeaway of the whole deck, then invite questions.

4. "persona": one or two sentences directing who naturally gives this talk and the tone to
   match, inferred from the subject matter - for example a technical deck suggests a
   working engineer explaining their own project; an academic deck suggests a teacher
   addressing students. Describe the voice and tone to use, not a named person. This is a
   private direction for the presenter and is never spoken aloud.

Style for intro, transitions, and outro - all spoken aloud:
- Plain flowing sentences. No markdown, bullets, emojis, or special characters.
- Do not mention slide numbers or the word "slide" mechanically; talk about the content
  ("Now that we've covered the problem, here's the fix", not "Moving to slide five").
- Use the deck's own key terms; do not invent facts beyond what the narration already says.

Reply with ONLY a JSON object:
{"intro": "...", "transitions": {"2": "...", "3": "...", ...}, "outro": "...", "persona": "..."}
The transitions keys are slide numbers as strings, covering every slide from 2 to the last."""


class NarrationModel(Protocol):
    """Produces slide narration from whatever material ingestion could extract."""

    #: Whether this implementation wants a rendered page image. Ingestion only
    #: pays the rasterisation cost when something will actually look at it.
    wants_image: bool

    async def narrate(
        self,
        *,
        text: str,
        slide_number: int,
        total: int,
        deck_title: str,
        image_png: bytes | None = None,
    ) -> dict[str, Any]:
        """Return a dict with title, bullets, narration and keywords."""
        ...


def _strip_fence(raw: str) -> str:
    """Models wrap JSON in ```json fences despite being told not to."""
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    return (m.group(1) if m else raw).strip()


def _coerce(parsed: Any, slide_number: int) -> dict[str, Any]:
    """Force whatever came back into the shape the rest of the app expects."""
    if not isinstance(parsed, dict):
        raise ValueError("narration model did not return a JSON object")

    title = str(parsed.get("title") or f"Slide {slide_number}").strip()

    bullets = [str(b).strip() for b in parsed.get("bullets") or [] if str(b).strip()]
    bullets = bullets[:4]

    narration = str(parsed.get("narration") or "").strip()
    if not narration:
        raise ValueError("narration model returned empty narration")

    keywords = [str(k).strip().lower() for k in parsed.get("keywords") or [] if str(k).strip()]

    return {
        "title": title,
        "bullets": bullets,
        "narration": narration,
        "keywords": keywords[:12],
    }


async def _with_artifact_correction(
    attempt: Callable[[str | None], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run ``attempt(None)``; retry once with a corrective instruction if the
    narration opened by describing the slide as an object.

    ``attempt`` takes an optional extra instruction to fold into the prompt on
    the retry. Keeps the first result if the retry itself fails or produces the
    same problem again - one clean pass beats a slide with no narration at all.
    """
    result = await attempt(None)
    if not _ARTIFACT_OPENER.match(result["narration"]):
        return result
    try:
        retry = await attempt(_STYLE_CORRECTION)
    except Exception:
        return result
    return result if _ARTIFACT_OPENER.match(retry["narration"]) else retry


class _ChatNarrationModel:
    """Shared OpenAI-compatible chat plumbing for both implementations."""

    wants_image = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 90.0,
        json_mode: bool = True,
    ):
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self._json_mode = json_mode

    def _user_text(self, *, text: str, slide_number: int, total: int, deck_title: str) -> str:
        return (
            f'Deck title: "{deck_title}"\n'
            f"Slide {slide_number} of {total}.\n\n"
            f"Extracted text:\n---\n{text.strip() or '(this slide has no extractable text)'}\n---"
        )

    async def _complete(self, content: Any, slide_number: int) -> dict[str, Any]:
        """Call the model, once, retrying a truncated or unparseable reply.

        The retry doubles the token budget rather than repeating the same
        request: the failure this guards against is the model running out of
        room, not the model being briefly unlucky.
        """
        last_error: Exception | None = None
        for attempt, budget in enumerate((NARRATION_MAX_TOKENS, NARRATION_MAX_TOKENS * 2)):
            try:
                return await self._attempt(content, slide_number, budget)
            except ValueError as exc:
                last_error = exc
                if attempt == 0:
                    logger.debug(
                        f"Slide {slide_number}: retrying with a larger token budget ({exc})"
                    )
        assert last_error is not None
        raise last_error

    async def _attempt(self, content: Any, slide_number: int, budget: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.4,
            "max_tokens": budget,
        }
        # Not every OpenAI-compatible server implements json_object mode; the
        # prompt already demands bare JSON and _strip_fence cleans up after it.
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with net_async_client(timeout=self._timeout) as client:
            resp = await client.post(
                self._url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            body = resp.json()

        choice = body["choices"][0]
        raw = choice["message"].get("content") or ""

        if choice.get("finish_reason") == "length":
            reasoning = (
                body.get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens")
            )
            raise ValueError(
                f"reply truncated at {budget} tokens "
                f"(reasoning used {reasoning}); content was {len(raw)} chars"
            )

        try:
            parsed = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f"narration model returned non-JSON: {raw[:200]!r}") from exc

        return _coerce(parsed, slide_number)


class TextNarrationModel(_ChatNarrationModel):
    """Narration from a slide's extracted text alone. The universal fallback."""

    wants_image = False

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 90.0):
        super().__init__(
            api_key=api_key, model=model, base_url=DEEPSEEK_BASE_URL, timeout=timeout
        )

    async def narrate(
        self,
        *,
        text: str,
        slide_number: int,
        total: int,
        deck_title: str,
        image_png: bytes | None = None,
    ) -> dict[str, Any]:
        """Narrate one slide. ``image_png`` is accepted and ignored."""

        async def attempt(extra: str | None) -> dict[str, Any]:
            user = self._user_text(
                text=text, slide_number=slide_number, total=total, deck_title=deck_title
            )
            if extra:
                user += f"\n\n{extra}"
            return await self._complete(user, slide_number)

        return await _with_artifact_correction(attempt)


class VisionNarrationModel(_ChatNarrationModel):
    """Narration from the rendered page image plus its extracted text.

    Speaks plain OpenAI-compatible chat completions with an ``image_url`` part,
    so it works against any endpoint serving a vision model - a self-hosted
    vLLM, OpenAI, or a provider's OpenAI-compatible layer.

    Falls back to text-only for a slide if the endpoint rejects the image, so a
    misconfigured vision setup degrades instead of failing the whole upload.
    """

    wants_image = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 120.0,
        json_mode: bool = True,
    ):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            json_mode=json_mode,
        )

    async def narrate(
        self,
        *,
        text: str,
        slide_number: int,
        total: int,
        deck_title: str,
        image_png: bytes | None = None,
    ) -> dict[str, Any]:
        """Narrate one slide from its image, falling back to text on refusal."""
        user_text = self._user_text(
            text=text, slide_number=slide_number, total=total, deck_title=deck_title
        )
        b64 = base64.b64encode(image_png).decode() if image_png else None

        async def attempt(extra: str | None) -> dict[str, Any]:
            prompt = user_text
            if image_png:
                prompt += (
                    "\n\nThe image is the rendered slide. Use it for anything the text "
                    "layer misses - charts, diagrams, tables, layout, labels."
                )
            if extra:
                prompt += f"\n\n{extra}"

            if b64:
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]
                try:
                    return await self._complete(content, slide_number)
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        f"Vision narration rejected for slide {slide_number} "
                        f"(HTTP {exc.response.status_code}); falling back to text only."
                    )

            return await self._complete(prompt, slide_number)

        return await _with_artifact_correction(attempt)


class BedrockVisionNarrationModel:
    """Narration from the rendered page image, via AWS Bedrock.

    Uses the Converse API, which normalises the request shape across model
    families - the same call works for Anthropic, Nova, Qwen-VL and the rest, so
    switching model is a config change.

    Two Bedrock specifics worth knowing:

    - Most current models cannot be invoked by their bare id. They need a
      cross-region *inference profile*, which is the same id with a region
      prefix: ``us.anthropic.claude-haiku-4-5-20251001-v1:0``. Calling the bare
      id returns ValidationException.
    - Model access is a per-account AWS Marketplace subscription. Without a
      valid payment method on file, every call fails with AccessDeniedException
      / INVALID_PAYMENT_INSTRUMENT regardless of IAM permissions - confirmed
      directly against this account, not assumed.

    Falls back to ``fallback`` (the text model) whenever a call fails, so a
    billing problem, a throttled account, or one bad page degrades to text
    narration instead of producing a deck full of placeholders.
    """

    wants_image = True

    def __init__(
        self,
        *,
        model_id: str,
        region: str,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        max_tokens: int = 2000,
        fallback: NarrationModel | None = None,
    ):
        # Imported here so boto3 is only required when Bedrock is actually used.
        import boto3

        self._model_id = model_id
        self._max_tokens = max_tokens
        self._fallback = fallback
        # Log the first failure loudly, then stay quiet - a throttled or
        # unbilled account would otherwise emit one identical warning per slide.
        self._warned = False
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            **(
                {
                    "aws_access_key_id": access_key_id,
                    "aws_secret_access_key": secret_access_key,
                }
                if access_key_id and secret_access_key
                else {}
            ),
        )

    def _converse(self, content: list[dict[str, Any]]) -> str:
        response = self._client.converse(
            modelId=self._model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": self._max_tokens, "temperature": 0.4},
        )
        return response["output"]["message"]["content"][0]["text"]

    async def narrate(
        self,
        *,
        text: str,
        slide_number: int,
        total: int,
        deck_title: str,
        image_png: bytes | None = None,
    ) -> dict[str, Any]:
        """Narrate one slide from its rendered image plus its text layer."""
        user_text = (
            f'Deck title: "{deck_title}"\n'
            f"Slide {slide_number} of {total}.\n\n"
            f"Extracted text:\n---\n{text.strip() or '(no extractable text)'}\n---"
        )
        instruction = (
            "\n\nThe image is the rendered slide. Read it for everything the text "
            "layer misses - charts, diagrams, tables, layout, labels, numbers on axes. "
            "Describe what the slide actually shows."
        )

        async def attempt(extra: str | None) -> dict[str, Any]:
            prompt = user_text + (instruction if image_png else "")
            if extra:
                prompt += f"\n\n{extra}"
            content: list[dict[str, Any]] = [{"text": prompt}]
            if image_png:
                content.append({"image": {"format": "png", "source": {"bytes": image_png}}})

            # boto3 is synchronous; keep it off the event loop.
            raw = await asyncio.to_thread(self._converse, content)
            parsed = json.loads(_strip_fence(raw))
            return _coerce(parsed, slide_number)

        try:
            return await _with_artifact_correction(attempt)
        except Exception as exc:
            if self._fallback is None:
                raise

            reason = type(exc).__name__
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if not self._warned:
                self._warned = True
                logger.warning(
                    f"Bedrock narration unavailable ({code or reason}); "
                    "falling back to text-only narration for this deck."
                )

            return await self._fallback.narrate(
                text=text,
                slide_number=slide_number,
                total=total,
                deck_title=deck_title,
            )


class GeminiVisionNarrationModel:
    """Narration from the rendered page image, via Google's Generative Language API.

    This is the default vision path. DeepSeek's platform API has no vision
    support at all, and Bedrock model access on the configured AWS account is
    blocked by AWS itself at ``INVALID_PAYMENT_INSTRUMENT`` (no payment method
    on file) - confirmed by a direct Converse call, not assumed. Gemini's REST
    API, in contrast, was verified end to end: a real image (a synthetic bar
    chart) sent to ``gemini-3.6-flash`` returns a correct description of it.

    A plain REST call rather than the ``google-genai`` SDK, to keep this
    module's dependency surface identical to the other implementations (just
    ``httpx``) and because the request shape is simple enough not to need it.

    Falls back to ``fallback`` (the text model) on any error, empty response,
    or safety block, so a quota hiccup or one bad page degrades to text
    narration instead of producing a deck full of placeholders.
    """

    wants_image = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout: float = 90.0,
        fallback: NarrationModel | None = None,
    ):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._fallback = fallback
        self._warned = False

    async def _generate(self, parts: list[dict[str, Any]], max_tokens: int) -> str:
        url = f"{GEMINI_BASE_URL}/models/{self._model}:generateContent"
        payload = {
            "contents": [{"parts": parts}],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.4,
                "responseMimeType": "application/json",
            },
        }
        async with net_async_client(timeout=self._timeout) as client:
            # The key travels as a header, never as a query parameter.
            # httpx embeds the full URL - query string included - in the
            # HTTPStatusError it raises, and that message gets logged on every
            # failure. With ?key= the API key ended up in plaintext in the
            # container logs; as a header it cannot.
            resp = await client.post(
                url, headers={"x-goog-api-key": self._api_key}, json=payload
            )
            resp.raise_for_status()
            body = resp.json()

        candidates = body.get("candidates") or []
        if not candidates:
            block = body.get("promptFeedback", {}).get("blockReason")
            raise ValueError(f"Gemini returned no candidates (blockReason={block})")

        candidate = candidates[0]
        parts_out = candidate.get("content", {}).get("parts", [])
        if not parts_out:
            raise ValueError(
                f"Gemini returned empty content (finishReason={candidate.get('finishReason')})"
            )
        return "".join(p.get("text", "") for p in parts_out)

    async def narrate(
        self,
        *,
        text: str,
        slide_number: int,
        total: int,
        deck_title: str,
        image_png: bytes | None = None,
    ) -> dict[str, Any]:
        """Narrate one slide from its rendered image plus its text layer."""
        user_text = (
            f'Deck title: "{deck_title}"\n'
            f"Slide {slide_number} of {total}.\n\n"
            f"Extracted text:\n---\n{text.strip() or '(no extractable text)'}\n---"
        )
        if image_png:
            user_text += (
                "\n\nThe attached image is the rendered slide. Read it for everything "
                "the text layer misses - charts, diagrams, tables, layout, labels, "
                "numbers on axes."
            )

        async def attempt(extra: str | None) -> dict[str, Any]:
            prompt = user_text + (f"\n\n{extra}" if extra else "")
            parts: list[dict[str, Any]] = [{"text": prompt}]
            if image_png:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": base64.b64encode(image_png).decode(),
                        }
                    }
                )
            raw = await self._generate(parts, NARRATION_MAX_TOKENS)
            try:
                parsed = json.loads(_strip_fence(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Gemini returned non-JSON: {raw[:200]!r}") from exc
            return _coerce(parsed, slide_number)

        try:
            return await _with_artifact_correction(attempt)
        except Exception as exc:
            if self._fallback is None:
                raise
            if not self._warned:
                self._warned = True
                logger.warning(
                    f"Gemini narration unavailable ({type(exc).__name__}: {exc}); "
                    "falling back to text-only narration for this deck."
                )
            return await self._fallback.narrate(
                text=text,
                slide_number=slide_number,
                total=total,
                deck_title=deck_title,
            )


def build_narration_model() -> NarrationModel:
    """Pick an implementation from the environment.

    Priority: Gemini, then Bedrock, then a generic OpenAI-compatible vision
    endpoint, then text-only. The first vision option with credentials wins;
    every one of them falls back to text narration on failure rather than
    raising, so a partially-working configuration still produces a deck.
    """
    text_fallback = TextNarrationModel(api_key=os.environ["DEEPSEEK_API_KEY"])

    gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("gemini_api_key") or "").strip()
    if gemini_key:
        logger.info(f"Narration: Gemini vision model {DEFAULT_GEMINI_MODEL}")
        return GeminiVisionNarrationModel(api_key=gemini_key, fallback=text_fallback)

    bedrock_model = os.environ.get("BEDROCK_MODEL_ID", "").strip()
    aws_key = (
        os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("aws_access_key_id") or ""
    ).strip()
    aws_secret = (
        os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("aws_secret_access_key") or ""
    ).strip()

    if bedrock_model and aws_key and aws_secret:
        region = os.environ.get("AWS_REGION", "us-east-1").strip()
        logger.info(f"Narration: Bedrock vision model {bedrock_model} in {region}")
        return BedrockVisionNarrationModel(
            model_id=bedrock_model,
            region=region,
            access_key_id=aws_key,
            secret_access_key=aws_secret,
            fallback=text_fallback,
        )

    vision_key = os.environ.get("VISION_API_KEY", "").strip()
    vision_model = os.environ.get("VISION_MODEL", "").strip()

    if vision_key and vision_model:
        base_url = os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1").strip()
        json_mode = os.environ.get("VISION_JSON_MODE", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        logger.info(f"Narration: vision model {vision_model} at {base_url}")
        return VisionNarrationModel(
            api_key=vision_key, model=vision_model, base_url=base_url, json_mode=json_mode
        )

    logger.info(
        f"Narration: text-only model {DEFAULT_MODEL} "
        "(set GEMINI_API_KEY, BEDROCK_MODEL_ID, or VISION_* to narrate from the page image)"
    )
    return text_fallback


async def title_for_deck(*, api_key: str, first_slides: str, filename: str) -> str:
    """Ask for a deck title from the opening slides, falling back to the filename."""
    fallback = re.sub(r"\.(pdf|pptx?)$", "", filename, flags=re.IGNORECASE)
    fallback = re.sub(r"[_-]+", " ", fallback).strip() or "Untitled deck"

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Give a short, plain title for this presentation, at most 8 words. "
                    "Reply with the title only - no quotes, no punctuation at the end, "
                    "no explanation."
                ),
            },
            {"role": "user", "content": first_slides[:4000]},
        ],
        "temperature": 0.3,
        "max_tokens": TITLE_MAX_TOKENS,
    }

    try:
        async with net_async_client(timeout=45.0) as client:
            resp = await client.post(
                DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            title = resp.json()["choices"][0]["message"]["content"].strip().strip('"')
        return title[:120] or fallback
    except Exception as exc:
        logger.warning(f"Could not derive a deck title, using the filename: {exc}")
        return fallback


async def synthesize_deck(
    *, api_key: str, deck_title: str, slides: list[dict[str, Any]]
) -> dict[str, Any]:
    """One pass over every slide's finished narration, producing the connective
    tissue that makes separately-narrated slides feel like one talk: an
    opening, a bridging line into every slide after the first, a closing, and a
    persona direction for how the presenter should sound.

    Best-effort by design, matching the reference implementation's philosophy:
    a deck without this is still fully presentable, just a little less polished
    on the seams. Callers should catch failures here and keep going.

    Args:
        api_key: DeepSeek key. Synthesis is pure text over already-written
            narration, so it never needs vision regardless of which model
            narrated the individual slides.
        deck_title: The deck's title, for context.
        slides: Each slide's ``{"id": int, "title": str, "narration": str}``.
    """
    lines = [f'Deck title: "{deck_title}"', f"Slides: {len(slides)}", ""]
    for s in slides:
        lines.append(f'Slide {s["id"]} — "{s["title"]}"')
        lines.append(f"  Narration: {s['narration']}")

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": 0.4,
        "max_tokens": SYNTHESIS_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    async with net_async_client(timeout=120.0) as client:
        resp = await client.post(
            DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()

    choice = body["choices"][0]
    raw = choice["message"].get("content") or ""
    if choice.get("finish_reason") == "length":
        raise ValueError(
            f"synthesis reply truncated at {SYNTHESIS_MAX_TOKENS} tokens - "
            "this deck may have too many slides for one synthesis pass"
        )

    parsed = json.loads(_strip_fence(raw))
    intro = str(parsed.get("intro") or "").strip()
    outro = str(parsed.get("outro") or "").strip()
    persona = str(parsed.get("persona") or "").strip()
    if not intro:
        raise ValueError("synthesis returned an empty intro")

    transitions: dict[int, str] = {}
    for key, value in (parsed.get("transitions") or {}).items():
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        text = str(value).strip()
        if text:
            transitions[n] = text

    return {"intro": intro, "outro": outro, "persona": persona, "transitions": transitions}
