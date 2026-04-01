"""Web search tool — OpenAI Responses API with LLM-first overridable defaults."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

DEFAULT_SEARCH_MODEL = "gpt-5.2"
DEFAULT_SEARCH_CONTEXT_SIZE = "medium"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_FALLBACK_PROVIDER = "duckduckgo"

_OPENAI_PRICING = {
    "gpt-5.2": (1.75, 14.0),
    "gpt-4.1": (2.0, 8.0),
    "o3": (2.0, 8.0),
    "o4-mini": (1.10, 4.40),
}


def _estimate_openai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost from token counts. Returns 0 if model pricing unknown."""
    pricing = _OPENAI_PRICING.get(model)
    if not pricing:
        for key, val in _OPENAI_PRICING.items():
            if key in model:
                pricing = val
                break
    if not pricing:
        pricing = (2.0, 10.0)
    input_price, output_price = pricing
    return round(input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000, 6)


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([.,!?;:])", r"\1", text)


def _normalize_duckduckgo_href(href: str) -> str:
    href = html.unescape((href or "").strip())
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/l/?"):
        try:
            parsed = urllib.parse.urlparse(href)
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
            return urllib.parse.unquote(target) if target else ""
        except Exception:
            return ""
    return ""


def _parse_duckduckgo_results(html_text: str) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="[^"]*result__a[^"]*"|$)',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text or ""):
        url = _normalize_duckduckgo_href(match.group(1))
        if not url or url in seen_urls:
            continue
        title = _clean_html_text(match.group(2))
        snippet = _clean_html_text(match.group(3))
        results.append({
            "title": title or url,
            "url": url,
            "snippet": snippet[:500],
        })
        seen_urls.add(url)
        if len(results) >= 10:
            return results

    fallback_pattern = re.compile(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in fallback_pattern.finditer(html_text or ""):
        url = _normalize_duckduckgo_href(match.group(1))
        if not url or url in seen_urls:
            continue
        title = _clean_html_text(match.group(2))
        if not title or len(title) < 3:
            continue
        results.append({
            "title": title,
            "url": url,
            "snippet": "",
        })
        seen_urls.add(url)
        if len(results) >= 10:
            break
    return results


def _duckduckgo_search(query: str, timeout: int = 20) -> Dict[str, Any]:
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/",
        data=data,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    results = _parse_duckduckgo_results(body)
    if not results:
        return {
            "provider": DEFAULT_FALLBACK_PROVIDER,
            "results": [],
            "error": "Fallback search returned no parseable results.",
        }
    answer_lines = [f"{idx + 1}. {item['title']} - {item['url']}" for idx, item in enumerate(results[:5])]
    return {
        "provider": DEFAULT_FALLBACK_PROVIDER,
        "results": results,
        "answer": "\n".join(answer_lines),
    }


def _web_search(
    ctx: ToolContext,
    query: str,
    model: str = "",
    search_context_size: str = "",
    reasoning_effort: str = "",
    provider: str = "auto",
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    want_openai = provider in ("", "auto", "openai")
    if not api_key and provider == "openai":
        return json.dumps({
            "error": "OPENAI_API_KEY not set. Configure it in Settings to enable web search."
        })

    active_model = model or os.environ.get("OUROBOROS_WEBSEARCH_MODEL", DEFAULT_SEARCH_MODEL)
    active_context = search_context_size or DEFAULT_SEARCH_CONTEXT_SIZE
    active_effort = reasoning_effort or DEFAULT_REASONING_EFFORT

    if api_key and want_openai:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            resp = client.responses.create(
                model=active_model,
                tools=[{
                    "type": "web_search",
                    "search_context_size": active_context,
                }],
                reasoning={"effort": active_effort},
                tool_choice="auto",
                input=query,
            )
            d = resp.model_dump()
            text = ""
            for item in d.get("output", []) or []:
                if item.get("type") == "message":
                    for block in item.get("content", []) or []:
                        if block.get("type") in ("output_text", "text"):
                            text += block.get("text", "")

            # Track web search cost (estimate from tokens — OpenAI usage has no total_cost)
            usage = d.get("usage") or {}
            if usage and hasattr(ctx, "pending_events"):
                input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                cost = _estimate_openai_cost(active_model, input_tokens, output_tokens)
                try:
                    ctx.pending_events.append({
                        "type": "llm_usage",
                        "provider": "openai_websearch",
                        "model": active_model,
                        "api_key_type": "openai",
                        "model_category": "websearch",
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                        "usage": usage,
                        "cost": cost,
                        "source": "web_search",
                        "ts": utc_now_iso(),
                        "category": "task",
                    })
                except Exception:
                    log.debug("Failed to emit web_search cost event", exc_info=True)

            return json.dumps({
                "provider": "openai",
                "answer": text or "(no answer)",
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            if provider == "openai":
                return json.dumps({"error": f"OpenAI web search failed: {repr(e)}"}, ensure_ascii=False)
            log.warning("OpenAI web search failed, falling back to %s: %s", DEFAULT_FALLBACK_PROVIDER, e)

    try:
        return json.dumps(_duckduckgo_search(query), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Fallback web search failed: {repr(e)}"}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": (
                "Search the web via OpenAI Responses API. "
                f"Defaults: model={DEFAULT_SEARCH_MODEL}, search_context_size={DEFAULT_SEARCH_CONTEXT_SIZE}, "
                f"reasoning_effort={DEFAULT_REASONING_EFFORT}. "
                "Override any parameter per-call if needed (LLM-first: you decide)."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"},
                "model": {"type": "string", "description": f"OpenAI model (default: {DEFAULT_SEARCH_MODEL})"},
                "search_context_size": {"type": "string", "enum": ["low", "medium", "high"],
                                        "description": f"How much context to fetch (default: {DEFAULT_SEARCH_CONTEXT_SIZE})"},
                "reasoning_effort": {"type": "string", "enum": ["low", "medium", "high"],
                                     "description": f"Reasoning effort (default: {DEFAULT_REASONING_EFFORT})"},
                "provider": {"type": "string", "enum": ["auto", "openai", "duckduckgo"],
                             "description": "Search backend. auto prefers OpenAI when configured, otherwise falls back to free DuckDuckGo HTML search."},
            }, "required": ["query"]},
        }, _web_search, timeout_sec=540),
    ]
