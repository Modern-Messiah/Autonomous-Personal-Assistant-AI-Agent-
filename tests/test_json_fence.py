"""Tests for markdown-fence stripping in LLM responses."""

from __future__ import annotations

from agent.tools.json_fence import strip_json_fence


def test_strips_multiline_json_fence() -> None:
    assert strip_json_fence('```json\n{"items": []}\n```') == '{"items": []}'


def test_strips_single_line_fence_without_leaving_language_tag() -> None:
    # the old strip("`") approach left the "json" tag glued to the payload
    assert strip_json_fence('```json{"city": null}```') == '{"city": null}'


def test_keeps_legitimate_backticks_inside_payload() -> None:
    payload = '{"summary": "код `предложение` в тексте"}'
    assert strip_json_fence(payload) == payload


def test_returns_plain_json_untouched() -> None:
    assert strip_json_fence('  {"ok": true}  ') == '{"ok": true}'
