import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


from ouroboros.structured_output import (
    clean_structured_text,
    extract_json_array,
    extract_json_object,
    strip_markdown_fences,
    strip_reasoning_artifacts,
)


def test_strip_reasoning_artifacts_removes_think_block():
    text = "<think>hidden</think>\n{\"ok\": true}"
    assert strip_reasoning_artifacts(text) == "{\"ok\": true}"


def test_strip_markdown_fences_removes_json_wrapper():
    text = "```json\n{\"ok\": true}\n```"
    assert strip_markdown_fences(text) == "{\"ok\": true}"


def test_clean_structured_text_handles_think_and_fences():
    text = "<think>hidden</think>\n```json\n{\"ok\": true}\n```"
    assert clean_structured_text(text) == "{\"ok\": true}"


def test_extract_json_object_from_minimax_style_output():
    text = "<think>reasoning</think>\n```json\n{\"status\": \"SAFE\", \"reason\": \"ok\"}\n```"
    assert extract_json_object(text) == {"status": "SAFE", "reason": "ok"}


def test_extract_json_array_from_minimax_style_output():
    text = "<think>reasoning</think>\n```json\n[{\"item\": \"x\"}]\n```"
    assert extract_json_array(text) == [{"item": "x"}]


def test_extract_json_object_finds_embedded_payload():
    text = "prefix\n<think>reasoning</think>\nResult:\n{\"value\": [1, 2, 3]}"
    assert extract_json_object(text) == {"value": [1, 2, 3]}
