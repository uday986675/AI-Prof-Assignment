"""LLM layer — provider selection with fallback, plus a deterministic fallback.

Two responsibilities, kept deliberately small:

1. ``build_llm`` — resolve the chat model: Gemini first when GEMINI_API_KEY is
   set, Groq second (settings.groq_api_key). An ``LLM_PROVIDER`` env var can
   force an explicit provider ("gemini" | "groq") when both keys exist. Returns
   None when no provider is configured; the agent then runs the deterministic
   fallback dialogue so the platform degrades gracefully instead of crashing.

2. ``interpret_utterance`` — translate ONE patient utterance into a structured
   update (specialty / when / appointment_type / slot_selected). The LLM is an
   input translator only: the slot-filling state machine (state.py) validates
   everything, so a hallucinated specialty/when value can never reach booking.

``interpret_utterance`` is best-effort by design: any LLM error (network,
auth, rate limit, malformed output) is swallowed and returns {} — the caller
then sees a state-machine question and the conversation continues.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..core.config import settings

logger = logging.getLogger("agent.llm")

_SYSTEM_PROMPT = """You translate a patient's message into JSON for a hospital \
appointment assistant. Reply with JSON ONLY (no prose, no markdown fence) using \
this shape:
{"specialty": string|null, "when": "today"|"tomorrow"|"this_week"|null, \
"appointment_type": "in_person"|"video"|null, "slot_selected": "ISO start_at"|null}
Rules:
- specialty: medical department the patient wants (e.g. "orthopedics"); null if \
the message does not name one. If they explicitly do not mind, use "".
- when: "today", "tomorrow" or "this_week" for relative dates; null otherwise.
- appointment_type: "in_person" or "video" if stated or clearly implied; null otherwise.
- slot_selected: the start_at of one of the offers ONLY if the patient picks one; \
otherwise null."""


class LLMUnavailableError(Exception):
    """No provider configured or all providers failed — the caller falls back."""


def _gemini_llm():
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model or "gemini-2.0-flash",
        google_api_key=settings.gemini_api_key,
        temperature=0,
        timeout=12,
        max_retries=1,
    )


def _groq_llm():
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=settings.groq_model or "llama-3.3-70b-versatile",
        api_key=settings.groq_api_key,
        temperature=0,
        timeout=12,
        max_retries=1,
    )


def build_llm():
    """Return a configured chat model, or None when nothing is usable.

    Order: explicit LLM_PROVIDER override, then Gemini (GEMINI_API_KEY),
    then Groq (GROQ_API_KEY). A provider that fails to CONSTRUCT falls through
    to the next one; invocation errors are handled by the caller (fallback).
    """
    forced = (getattr(settings, "llm_provider", "") or "").strip().lower()
    if forced in ("gemini", "groq"):
        try:
            return (_gemini_llm if forced == "gemini" else _groq_llm)()
        except Exception as exc:  # provider init failed — try the other one
            logger.warning("%s LLM unavailable (%s); trying the next provider", forced.capitalize(), exc)
            forced = ""

    builders = (
        ("Gemini", _gemini_llm, settings.gemini_api_key),
        ("Groq", _groq_llm, settings.groq_api_key),
    )
    if forced:  # an explicitly requested provider failed to initialize
        return None
    for label, builder, api_key in builders:
        if api_key:
            try:
                return builder()
            except Exception as exc:
                logger.warning("%s LLM unavailable (%s); trying the next provider", label, exc)
    return None


def _content_to_text(content: Any) -> str:
    """Flatten a chat-model response body to plain text.

    Gemini 3 responses arrive as a list of content blocks
    ([{'type': 'text', 'text': ...}, ...]); Groq/OpenAI-style models return a
    plain string. Handle both (plus a nested list inside a block, defensively).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _coerce_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("` \n")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def interpret_utterance(message: str, offered: list[dict] | None = None) -> dict[str, Any]:
    """Translate one utterance into a state update; {} on any LLM problem."""
    offered = offered or []
    llm = build_llm()
    if llm is None:
        raise LLMUnavailableError("no LLM provider configured")
    prompt = _SYSTEM_PROMPT
    if offered:
        offered_lines = "\n".join(
            f"- {o['start_at']} with {o['doctor_name']} ({o['specialty']})" for o in offered
        )
        prompt += f"\nCurrently offered slots:\n{offered_lines}"
    try:
        response = llm.invoke(
            [
                ("system", prompt),
                ("human", message),
            ]
        )
    except Exception as exc:
        logger.warning("LLM invocation failed (%s); using fallback dialogue", exc)
        raise LLMUnavailableError(str(exc)) from exc

    content = response.content if hasattr(response, "content") else response
    update = _coerce_json(_content_to_text(content))
    # Keep only known keys — the state machine owns validation.
    return {k: update[k] for k in ("specialty", "when", "appointment_type", "slot_selected") if k in update}
