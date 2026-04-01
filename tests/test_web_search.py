import json
import pathlib
import tempfile

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.search import _parse_duckduckgo_results, _web_search


def _make_ctx():
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolContext(repo_dir=tmp, drive_root=tmp)


def test_parse_duckduckgo_results_extracts_title_url_and_snippet():
    raw_html = """
    <div class="result">
      <a class="result__a" href="//example.com/page">Example Title</a>
      <a class="result__snippet">Example snippet <b>here</b>.</a>
    </div>
    """
    results = _parse_duckduckgo_results(raw_html)
    assert len(results) == 1
    assert results[0]["title"] == "Example Title"
    assert results[0]["url"] == "https://example.com/page"
    assert "Example snippet here." in results[0]["snippet"]


def test_web_search_falls_back_without_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "ouroboros.tools.search._duckduckgo_search",
        lambda query, timeout=20: {
            "provider": "duckduckgo",
            "results": [{"title": "Example", "url": "https://example.com", "snippet": "Snippet"}],
            "answer": "1. Example - https://example.com",
        },
    )
    result = json.loads(_web_search(_make_ctx(), "example query"))
    assert result["provider"] == "duckduckgo"
    assert result["results"][0]["url"] == "https://example.com"


def test_web_search_openai_provider_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = json.loads(_web_search(_make_ctx(), "example query", provider="openai"))
    assert "error" in result
    assert "OPENAI_API_KEY not set" in result["error"]
