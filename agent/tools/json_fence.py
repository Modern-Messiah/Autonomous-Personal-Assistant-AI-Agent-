"""Shared helpers for LLM responses that may arrive fenced in markdown code blocks."""

from __future__ import annotations

import re

# ```json ... ``` (optionally with a language tag), possibly surrounded by prose.
_FENCE_PREFIX = re.compile(r"^```[\w+-]*[ \t]*\r?\n?")
_FENCE_SUFFIX = re.compile(r"\r?\n?[ \t]*```$")


def strip_json_fence(content: str) -> str:
    """Strip a surrounding markdown code fence from an LLM response.

    Some chat models wrap a JSON object in ```json ... ``` despite
    ``response_format``. The old ``strip("`")`` approach also ate legitimate
    backticks at the very start/end of the payload and left a stray ``json``
    language tag on single-line responses, turning a valid answer into a
    "malformed output" retry (a wasted paid API call).
    """
    cleaned = content.strip()
    if not cleaned.startswith("```"):
        return cleaned
    cleaned = _FENCE_PREFIX.sub("", cleaned)
    cleaned = _FENCE_SUFFIX.sub("", cleaned)
    return cleaned.strip()
