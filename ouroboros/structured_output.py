"""Helpers for parsing model outputs that may include reasoning artifacts."""

from __future__ import annotations

import json
import re
from typing import Any, Optional


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def strip_reasoning_artifacts(text: Any) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_markdown_fences(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    match = _FENCE_RE.fullmatch(raw)
    if match:
        return match.group(1).strip()

    if raw.startswith("```"):
        return raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    return raw


def clean_structured_text(text: Any) -> str:
    return strip_markdown_fences(strip_reasoning_artifacts(text))


def _extract_json_fragment(text: str, open_char: str, close_char: str) -> Optional[str]:
    depth = 0
    start: Optional[int] = None
    in_string = False
    escape = False

    for idx, char in enumerate(text):
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == open_char:
            if depth == 0:
                start = idx
            depth += 1
        elif char == close_char and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                return text[start:idx + 1]
    return None


def extract_json_object(text: Any) -> Optional[dict]:
    cleaned = clean_structured_text(text)
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    fragment = _extract_json_fragment(cleaned, "{", "}")
    if not fragment:
        return None
    try:
        obj = json.loads(fragment)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_json_array(text: Any) -> Optional[list]:
    cleaned = clean_structured_text(text)
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    fragment = _extract_json_fragment(cleaned, "[", "]")
    if not fragment:
        return None
    try:
        obj = json.loads(fragment)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return obj if isinstance(obj, list) else None
