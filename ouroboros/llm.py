"""
Ouroboros — LLM client.

The only module that communicates with LLM APIs (OpenRouter + optional local).
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import copy
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.config import (
    get_cloud_provider,
    has_local_model_config,
    has_minimax_config,
    resolve_local_model_api_key,
    resolve_local_model_base_url,
    resolve_minimax_base_url,
    resolve_openrouter_base_url,
)

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "anthropic/claude-sonnet-4.6"


class LocalContextTooLargeError(RuntimeError):
    """Raised when a local model cannot fit context without silent truncation."""


class LocalBackendUnavailableError(RuntimeError):
    """Raised when local LLM backend is unreachable."""


def _estimate_message_chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            total += sum(len(str(block.get("text", ""))) for block in content if isinstance(block, dict))
        else:
            total += len(str(content or ""))
    return total


def _split_markdown_sections(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = str(text or "").splitlines()
    preamble: List[str] = []
    sections: List[Tuple[str, str]] = []
    current_title: Optional[str] = None
    current_lines: List[str] = []

    for line in lines:
        if line.startswith("## "):
            if current_title is None:
                preamble = current_lines[:]
            else:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_title is None:
        return "\n".join(lines).strip(), []

    sections.append((current_title, "\n".join(current_lines).strip()))
    return "\n".join(preamble).strip(), sections


def _compact_markdown_sections(
    text: str,
    preserve_titles: Set[str],
    reason: str,
) -> str:
    preamble, sections = _split_markdown_sections(text)
    if not sections:
        return text

    parts: List[str] = []
    if preamble:
        parts.append(preamble)

    for title, section in sections:
        if title in preserve_titles:
            parts.append(section)
            continue
        omitted_chars = max(0, len(section))
        parts.append(
            f"## {title}\n\n"
            f"[Compacted for local-model context: omitted {omitted_chars} chars. {reason}]"
        )

    return "\n\n".join(p for p in parts if p).strip()


def _compact_local_static_text(text: str) -> str:
    return _compact_markdown_sections(
        text,
        preserve_titles={"BIBLE.md"},
        reason="Use a larger-context model or read the source file directly if this section becomes necessary.",
    )


def _compact_local_semi_stable_text(text: str) -> str:
    return _compact_markdown_sections(
        text,
        preserve_titles={"Scratchpad", "Identity"},
        reason="Scratchpad and Identity were preserved; non-core memory sections were compacted for local execution.",
    )


def _compact_local_dynamic_text(text: str) -> str:
    return _compact_markdown_sections(
        text,
        preserve_titles={"Drive state", "Runtime context", "Health Invariants"},
        reason="Recent/history-heavy sections were compacted for local execution.",
    )


def _compact_local_system_text(text: str) -> str:
    return _compact_markdown_sections(
        text,
        preserve_titles={
            "BIBLE.md",
            "Scratchpad",
            "Identity",
            "Drive state",
            "Runtime context",
            "Health Invariants",
            "Recent observations",
            "Background consciousness info",
        },
        reason="Non-core sections were compacted for local execution.",
    )


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Fetch current pricing from OpenRouter API.

    Returns dict of {model_id: (input_per_1m, cached_per_1m, output_per_1m)}.
    Returns empty dict on failure.
    """
    import logging
    log = logging.getLogger("ouroboros.llm")

    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = f"{resolve_openrouter_base_url()}/models"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        # Prefixes we care about
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")

        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue

            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue

            # OpenRouter pricing is in dollars per token (raw values)
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None

            # Convert to per-million tokens
            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            if raw_cached is not None:
                cached_price = round(raw_cached * 1_000_000, 4)
            else:
                cached_price = round(prompt_price * 0.1, 4)  # fallback: 10% of prompt

            # Sanity check: skip obviously wrong prices
            if prompt_price > 1000 or completion_price > 1000:
                log.warning(f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})")
                continue

            pricing_dict[model_id] = (prompt_price, cached_price, completion_price)

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


class LLMClient:
    """LLM API wrapper. Routes calls to OpenRouter or a local llama-cpp-python server."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self._api_key_override = api_key
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url_override = base_url
        self._base_url = resolve_openrouter_base_url(base_url)
        self._provider_override: Optional[str] = str(provider).strip().lower() if provider else None
        self._client = None
        self._client_api_key: Optional[str] = None
        self._client_base_url: Optional[str] = None
        self._client_provider: Optional[str] = None
        self._async_client = None
        self._async_client_api_key: Optional[str] = None
        self._async_client_base_url: Optional[str] = None
        self._async_client_provider: Optional[str] = None

    def _resolve_openrouter_base_url(self) -> str:
        return resolve_openrouter_base_url(self._base_url_override)

    def _resolve_minimax_base_url(self) -> str:
        return resolve_minimax_base_url(self._base_url_override)

    def _get_cloud_provider(self) -> str:
        if self._provider_override:
            return self._provider_override
        return get_cloud_provider()

    def cloud_provider(self) -> str:
        """Return the currently configured cloud provider."""
        return self._get_cloud_provider()

    def _get_cloud_api_key(self, provider: Optional[str] = None) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        resolved = provider or self._get_cloud_provider()
        if resolved == "minimax":
            return os.environ.get("MINIMAX_API_KEY", "")
        return os.environ.get("OPENROUTER_API_KEY", "")

    def _resolve_cloud_base_url(self, provider: Optional[str] = None) -> str:
        resolved = provider or self._get_cloud_provider()
        if resolved == "minimax":
            return self._resolve_minimax_base_url()
        return self._resolve_openrouter_base_url()

    @staticmethod
    def _cloud_client_headers(provider: str) -> Optional[Dict[str, str]]:
        if provider == "openrouter":
            return {
                "HTTP-Referer": "https://ouroboros.local/",
                "X-Title": "Ouroboros",
            }
        return None

    def _get_client(self):
        provider = self._get_cloud_provider()
        current_api_key = self._get_cloud_api_key(provider)
        current_base_url = self._resolve_cloud_base_url(provider)

        if (
            self._client is None
            or self._client_api_key != current_api_key
            or self._client_base_url != current_base_url
            or self._client_provider != provider
        ):
            from openai import OpenAI
            kwargs: Dict[str, Any] = {
                "base_url": current_base_url,
                "api_key": current_api_key,
                "max_retries": 0,
            }
            headers = self._cloud_client_headers(provider)
            if headers:
                kwargs["default_headers"] = headers
            self._client = OpenAI(**kwargs)
            self._client_api_key = current_api_key
            self._client_base_url = current_base_url
            self._client_provider = provider
            self._api_key = current_api_key
            self._base_url = current_base_url
        return self._client

    def _get_async_client(self):
        provider = self._get_cloud_provider()
        current_api_key = self._get_cloud_api_key(provider)
        current_base_url = self._resolve_cloud_base_url(provider)

        if (
            self._async_client is None
            or self._async_client_api_key != current_api_key
            or self._async_client_base_url != current_base_url
            or self._async_client_provider != provider
        ):
            from openai import AsyncOpenAI
            kwargs: Dict[str, Any] = {
                "base_url": current_base_url,
                "api_key": current_api_key,
                "max_retries": 0,
            }
            headers = self._cloud_client_headers(provider)
            if headers:
                kwargs["default_headers"] = headers
            self._async_client = AsyncOpenAI(**kwargs)
            self._async_client_api_key = current_api_key
            self._async_client_base_url = current_base_url
            self._async_client_provider = provider
        return self._async_client

    @staticmethod
    def _strip_cache_control(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Strip cache_control from message content blocks (OpenRouter/Anthropic-only)."""
        import copy
        cleaned = copy.deepcopy(messages)
        for msg in cleaned:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        return cleaned

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API as fallback."""
        try:
            import requests
            url = f"{self._resolve_openrouter_base_url()}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost from OpenRouter", exc_info=True)
            pass
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
        use_local: bool = False,
        temperature: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost).

        When use_local=True, routes to the local llama-cpp-python server
        and strips OpenRouter-specific parameters (reasoning, provider, cache_control).
        """
        provider = self._get_cloud_provider()
        current_api_key = self._get_cloud_api_key(provider)
        should_fallback_to_local = (
            not use_local
            and not str(current_api_key or "").strip()
            and has_local_model_config()
        )

        if use_local or should_fallback_to_local:
            if should_fallback_to_local:
                log.info(
                    "OPENROUTER_API_KEY is not set; routing chat() call to local backend "
                    "for model '%s'",
                    model,
                )
            return self._chat_local(
                messages=messages,
                model=model,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
            )

        return self._chat_cloud(
            provider=provider,
            messages=messages,
            model=model,
            tools=tools,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            temperature=temperature,
        )

    async def chat_async(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Async OpenRouter chat used by review/concurrent callers."""
        if tools:
            raise ValueError("chat_async does not support tool calls")
        provider = self._get_cloud_provider()
        client = self._get_async_client()
        kwargs = self._build_cloud_kwargs(
            provider, messages, model, tools, reasoning_effort, max_tokens, tool_choice, temperature
        )
        resp = await client.chat.completions.create(**kwargs)
        return self._normalize_cloud_response(provider, resp.model_dump())

    def _prepare_messages_for_local_context(
        self,
        messages: List[Dict[str, Any]],
        ctx_len: int,
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        available_tokens = max(256, ctx_len - max_tokens - 64)
        target_chars = available_tokens * 3
        total_chars = _estimate_message_chars(messages)
        if total_chars <= target_chars:
            return messages

        compacted = copy.deepcopy(messages)
        for msg in compacted:
            if msg.get("role") != "system":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for idx, block in enumerate(content):
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    block_text = str(block.get("text", ""))
                    if idx == 0:
                        block["text"] = _compact_local_static_text(block_text)
                    elif idx == 1:
                        block["text"] = _compact_local_semi_stable_text(block_text)
                    else:
                        block["text"] = _compact_local_dynamic_text(block_text)
            elif isinstance(content, str):
                msg["content"] = _compact_local_system_text(content)
            break

        compacted_chars = _estimate_message_chars(compacted)
        if compacted_chars <= target_chars:
            return compacted

        raise LocalContextTooLargeError(
            f"Local model context too large after safe compaction "
            f"({compacted_chars} chars > target {target_chars})."
        )

    @staticmethod
    def _normalize_local_message_order(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensure backend-compatible ordering and shape for strict proxies.

        Some OpenAI-compatible proxies require exactly one system message at
        index 0. We therefore move all system messages to the beginning and
        collapse them into a single system block.
        """
        system_texts: List[str] = []
        other_msgs: List[Dict[str, Any]] = []
        seen_non_system = False
        had_late_system = False

        for msg in messages:
            if str(msg.get("role", "")).strip() == "system":
                system_texts.append(str(msg.get("content", "") or ""))
                if seen_non_system:
                    had_late_system = True
            else:
                seen_non_system = True
                other_msgs.append(msg)

        if not system_texts:
            return other_msgs

        combined_system = {
            "role": "system",
            "content": "\n\n".join(part for part in system_texts if part).strip(),
        }

        if had_late_system or len(system_texts) > 1:
            log.info(
                "Normalized local chat payload: collapsed %d system message(s) into one at index 0",
                len(system_texts),
            )
        return [combined_system] + other_msgs

    def _chat_local(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        max_tokens: int,
        tool_choice: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to the local llama-cpp-python server."""
        import requests

        clean_messages = self._strip_cache_control(messages)
        # Flatten multipart content blocks to plain strings (local server doesn't support arrays)
        local_max = min(max_tokens, 2048)
        ctx_len = 0
        base_url = resolve_local_model_base_url()
        explicit_base_url = bool(str(os.environ.get("LOCAL_MODEL_BASE_URL", "")).strip())
        try:
            if not explicit_base_url:
                from ouroboros.local_model import get_manager
                ctx_len = get_manager().get_context_length()
                if ctx_len > 0:
                    local_max = min(max_tokens, max(256, ctx_len // 4))
        except Exception:
            pass

        if ctx_len > 0:
            clean_messages = self._prepare_messages_for_local_context(clean_messages, ctx_len, local_max)

        for msg in clean_messages:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = "\n\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
        clean_messages = self._normalize_local_message_order(clean_messages)

        clean_tools = None
        if tools:
            clean_tools = [
                {k: v for k, v in t.items() if k != "cache_control"}
                for t in tools
            ]

        requested_model = str(model or "").strip() or "local-model"
        kwargs: Dict[str, Any] = {
            "model": requested_model,
            "messages": clean_messages,
            "max_tokens": local_max,
        }
        if clean_tools:
            kwargs["tools"] = clean_tools
            kwargs["tool_choice"] = tool_choice
        headers: Dict[str, str] = {}
        local_api_key = resolve_local_model_api_key().strip()
        if local_api_key:
            headers["Authorization"] = f"Bearer {local_api_key}"
        endpoint = f"{base_url}/chat/completions"

        last_exc: Optional[Exception] = None
        last_conn_err: Optional[Exception] = None
        resp_dict: Dict[str, Any] = {}
        tools_disabled_for_compat = False
        for attempt in range(3):
            try:
                resp = requests.post(endpoint, json=copy.deepcopy(kwargs), headers=headers, timeout=120)
                status_code = int(getattr(resp, "status_code", 0) or 0)
                if 400 <= status_code < 500 and self._is_local_model_not_found(resp):
                    fallback_model = self._choose_local_fallback_model(
                        requested_model=requested_model,
                        current_model=str(kwargs.get("model") or "").strip(),
                        base_url=base_url,
                        headers=headers,
                    )
                    if fallback_model and fallback_model != kwargs.get("model"):
                        kwargs["model"] = fallback_model
                        log.warning(
                            "Local backend model '%s' not found at %s; retrying with '%s'",
                            requested_model,
                            base_url,
                            fallback_model,
                        )
                        time.sleep(0.2)
                        continue
                if 400 <= status_code < 500:
                    error_text = self._extract_local_error_text(resp)
                    if self._is_local_context_error_text(error_text):
                        self._shrink_messages_from_error(clean_messages, error_text)
                        kwargs["messages"] = clean_messages
                        log.warning(
                            "Local backend rejected request due to context length for model '%s'; "
                            "shrinking messages and retrying",
                            str(kwargs.get("model") or "").strip(),
                        )
                        time.sleep(0.2)
                        continue
                    if clean_tools and not tools_disabled_for_compat and self._is_local_tools_unsupported_text(error_text):
                        tools_disabled_for_compat = True
                        clean_tools = None
                        kwargs.pop("tools", None)
                        kwargs.pop("tool_choice", None)
                        log.warning(
                            "Local backend rejected tools payload for model '%s'; "
                            "retrying without tools",
                            str(kwargs.get("model") or "").strip(),
                        )
                        time.sleep(0.2)
                        continue
                resp.raise_for_status()
                resp_dict = resp.json()
                last_exc = None
                last_conn_err = None
                break
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, requests.exceptions.ConnectionError):
                    last_conn_err = exc
                err = str(exc)
                resp = getattr(exc, "response", None)
                if resp is not None:
                    backend_err = self._extract_local_error_text(resp)
                    if backend_err and backend_err not in err.lower():
                        err = f"{err} | backend_error={backend_err[:500]}"
                if "context_length_exceeded" in err:
                    raise LocalContextTooLargeError(err) from exc
                if attempt >= 2:
                    log.warning("Local model request failed: %s", err)
                    break
                time.sleep(0.2)
        if last_conn_err is not None:
            raise LocalBackendUnavailableError(
                "Local LLM backend is unreachable at "
                f"{endpoint}. Start a local server on that address/port "
                "or set LOCAL_MODEL_BASE_URL to a reachable OpenAI-compatible endpoint."
            ) from last_conn_err
        if last_exc is not None:
            raise last_exc

        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        if not msg.get("tool_calls") and msg.get("content") and clean_tools:
            allowed_tool_names = {
                str(t.get("function", {}).get("name", "")).strip()
                for t in clean_tools
                if isinstance(t, dict)
            }
            msg = self._parse_tool_calls_from_content(msg, allowed_tool_names)

        usage["cost"] = 0.0
        return msg, usage

    @staticmethod
    def _extract_local_error_text(response: Any) -> str:
        try:
            payload = response.json()
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""

        chunks: List[str] = []
        for key in ("type", "code", "message", "detail"):
            val = payload.get(key)
            if val is not None:
                chunks.append(str(val))

        nested = payload.get("error")
        if isinstance(nested, dict):
            for key in ("type", "code", "message", "detail"):
                val = nested.get(key)
                if val is not None:
                    chunks.append(str(val))

        return " | ".join(part.strip() for part in chunks if str(part).strip()).lower()

    @staticmethod
    def _is_local_context_error_text(error_text: str) -> bool:
        text = str(error_text or "").lower()
        return (
            "context_length_exceeded" in text
            or ("maximum context length" in text and "requested" in text)
            or ("too many tokens" in text)
        )

    @staticmethod
    def _is_local_tools_unsupported_text(error_text: str) -> bool:
        text = str(error_text or "").lower()
        return (
            ("tool" in text and ("unsupported" in text or "not supported" in text))
            or ("invalid" in text and "tool_choice" in text)
            or ("invalid" in text and "tools" in text)
        )

    @staticmethod
    def _is_local_model_not_found(response: Any) -> bool:
        """Best-effort detection of model-not-found payloads."""
        try:
            payload = response.json()
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False

        err_type = str(payload.get("type", "")).strip().lower()
        err_msg = str(payload.get("message", "")).strip().lower()
        if err_type == "model_not_found" or ("model" in err_msg and "not found" in err_msg):
            return True

        # Some OpenAI-compatible backends wrap errors under "error".
        nested = payload.get("error")
        if isinstance(nested, dict):
            nested_type = str(nested.get("type", "")).strip().lower()
            nested_code = str(nested.get("code", "")).strip().lower()
            nested_msg = str(nested.get("message", "")).strip().lower()
            if (
                nested_type == "model_not_found"
                or nested_code == "model_not_found"
                or ("invalid model" in nested_msg)
                or ("model" in nested_msg and "not found" in nested_msg)
            ):
                return True

        return False

    def _choose_local_fallback_model(
        self,
        requested_model: str,
        current_model: str,
        base_url: str,
        headers: Dict[str, str],
    ) -> Optional[str]:
        """Pick a compatible local model from /models when requested one is unavailable."""
        try:
            import requests

            resp = requests.get(f"{base_url}/models", headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json() if isinstance(resp.json(), dict) else {}
            items = data.get("data", []) or []
            available = [
                str(item.get("id", "")).strip()
                for item in items
                if isinstance(item, dict) and str(item.get("id", "")).strip()
            ]
        except Exception:
            return None

        if not available:
            return None
        if requested_model in available:
            return requested_model

        preferred = [
            str(os.environ.get("OUROBOROS_MODEL", "")).strip(),
            str(os.environ.get("OUROBOROS_MODEL_LIGHT", "")).strip(),
            str(os.environ.get("OUROBOROS_MODEL_CODE", "")).strip(),
            "Qwen/Qwen3.5-27B",
        ]
        for candidate in preferred:
            if candidate and candidate != current_model and candidate in available:
                return candidate

        for candidate in available:
            if candidate != current_model:
                return candidate
        return None

    @staticmethod
    def _parse_tool_calls_from_content(
        msg: Dict[str, Any],
        allowed_tool_names: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Parse <tool_call> XML tags from content into structured tool_calls.

        Works around llama-cpp-python not parsing Qwen/Hermes-style tool calls
        (https://github.com/abetlen/llama-cpp-python/issues/1784).
        """
        content = str(msg.get("content", "") or "")
        stripped = content.strip()
        if not stripped:
            return msg

        # Safety: only upgrade the response when it consists solely of
        # one or more <tool_call> blocks. If the model mixed prose with
        # examples or explanations, leave it as plain text.
        full_pattern = re.compile(
            r"^(?:\s*<tool_call>\s*\{.*?\}\s*</tool_call>\s*)+$",
            re.DOTALL,
        )
        if not full_pattern.fullmatch(stripped):
            return msg

        matches = re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", stripped, re.DOTALL)
        if not matches:
            return msg

        allowed = {name for name in (allowed_tool_names or set()) if name}
        tool_calls = []
        for i, raw in enumerate(matches):
            try:
                raw_stripped = raw.strip()
                try:
                    obj = json.loads(raw_stripped)
                except json.JSONDecodeError:
                    if raw_stripped.startswith("{{") and raw_stripped.endswith("}}"):
                        obj = json.loads(raw_stripped[1:-1])
                    else:
                        raise
                if not isinstance(obj, dict):
                    raise ValueError("tool_call payload must be an object")
                name = str(obj.get("name", "")).strip()
                args = obj.get("arguments", {})
                if not name:
                    raise ValueError("tool_call missing function name")
                if allowed and name not in allowed:
                    raise ValueError(f"unknown tool '{name}'")
                if not isinstance(args, dict):
                    raise ValueError("tool_call arguments must be an object")
                tool_calls.append({
                    "id": f"call_local_{i}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                })
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("Rejected local <tool_call> block: %s (%s)", raw[:200], exc)
                return msg

        if not tool_calls:
            return msg

        msg = dict(msg)
        msg["tool_calls"] = tool_calls
        msg["content"] = None
        log.info("Parsed %d local tool call(s) from text output", len(tool_calls))
        return msg

    @staticmethod
    def _truncate_messages_for_context(
        messages: List[Dict[str, Any]], ctx_len: int, max_tokens: int,
    ) -> None:
        """Hard-truncate message content so total fits within the context window.

        Uses a conservative 3-chars-per-token ratio to avoid underestimating.
        """
        available_tokens = ctx_len - max_tokens - 64
        if available_tokens < 256:
            available_tokens = 256
        target_chars = available_tokens * 3

        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if total_chars <= target_chars:
            return

        for msg in messages:
            if msg["role"] == "system" and isinstance(msg.get("content"), str):
                content = msg["content"]
                other_chars = total_chars - len(content)
                allowed = max(512, target_chars - other_chars)
                if len(content) > allowed:
                    msg["content"] = content[:allowed] + "\n\n[Context truncated to fit model window]"
                    log.info("Truncated system message from %d to %d chars for %d-token context",
                             len(content), allowed, ctx_len)
                return

    @staticmethod
    def _shrink_messages_from_error(
        messages: List[Dict[str, Any]], error_text: str,
    ) -> None:
        """Parse a context_length_exceeded error and shrink the largest message."""
        m = re.search(r"requested (\d+) tokens.*?(\d+) in the messages", error_text)
        if not m:
            for msg in messages:
                if msg["role"] == "system" and isinstance(msg.get("content"), str):
                    msg["content"] = msg["content"][:len(msg["content"]) // 2]
                    return
            return

        requested = int(m.group(1))
        msg_tokens = int(m.group(2))
        # Find max context from "maximum context length is N tokens"
        ctx_match = re.search(r"maximum context length is (\d+)", error_text)
        ctx_max = int(ctx_match.group(1)) if ctx_match else 16384
        comp_match = re.search(r"(\d+) in the completion", error_text)
        comp_tokens = int(comp_match.group(1)) if comp_match else 2048

        target_msg_tokens = ctx_max - comp_tokens - 64
        if target_msg_tokens < 256:
            target_msg_tokens = 256
        ratio = target_msg_tokens / max(msg_tokens, 1)
        if ratio >= 1.0:
            ratio = 0.5

        for msg in messages:
            if msg["role"] == "system" and isinstance(msg.get("content"), str):
                content = msg["content"]
                new_len = max(512, int(len(content) * ratio))
                if new_len < len(content):
                    msg["content"] = content[:new_len] + "\n\n[Context truncated to fit model window]"
                    log.info("Retry-truncated system message to %d chars (ratio=%.2f)", new_len, ratio)
                return

    def _build_openrouter_kwargs(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float],
    ) -> Dict[str, Any]:
        effort = normalize_reasoning_effort(reasoning_effort)

        extra_body: Dict[str, Any] = {
            "reasoning": {"effort": effort, "exclude": True},
        }

        if model.startswith("anthropic/"):
            extra_body["provider"] = {
                "require_parameters": True,
            }

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            tools_with_cache = [t for t in tools]  # shallow copy
            if tools_with_cache:
                last_tool = {**tools_with_cache[-1]}  # copy last tool
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_with_cache[-1] = last_tool
            kwargs["tools"] = tools_with_cache
            kwargs["tool_choice"] = tool_choice
        return kwargs

    def _build_minimax_kwargs(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float],
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": self._strip_cache_control(messages),
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [
                {k: v for k, v in tool.items() if k != "cache_control"}
                for tool in tools
            ]
            kwargs["tool_choice"] = tool_choice
        return kwargs

    def _build_cloud_kwargs(
        self,
        provider: str,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float],
    ) -> Dict[str, Any]:
        if provider == "minimax":
            return self._build_minimax_kwargs(
                messages, model, tools, reasoning_effort, max_tokens, tool_choice, temperature
            )
        return self._build_openrouter_kwargs(
            messages, model, tools, reasoning_effort, max_tokens, tool_choice, temperature
        )

    def _normalize_openrouter_response(
        self,
        resp_dict: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (prompt_details_for_write.get("cache_write_tokens")
                              or prompt_details_for_write.get("cache_creation_tokens")
                              or prompt_details_for_write.get("cache_creation_input_tokens"))
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        if not usage.get("cost"):
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id)
                if cost is not None:
                    usage["cost"] = cost

        return msg, usage

    def _normalize_cloud_response(
        self,
        provider: str,
        resp_dict: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if provider == "minimax":
            usage = resp_dict.get("usage") or {}
            choices = resp_dict.get("choices") or [{}]
            msg = (choices[0] if choices else {}).get("message") or {}
            return msg, usage
        return self._normalize_openrouter_response(resp_dict)

    def _chat_cloud(
        self,
        provider: str,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to the selected cloud provider."""
        current_api_key = self._get_cloud_api_key(provider)
        if not str(current_api_key or "").strip():
            if has_local_model_config():
                raise RuntimeError(
                    "Cloud LLM backend is not configured. "
                    "Local model is configured; route this call through local backend."
                )
            missing_key = "MINIMAX_API_KEY" if provider == "minimax" else "OPENROUTER_API_KEY"
            raise RuntimeError(
                f"No LLM provider configured. Configure {missing_key} "
                "or local model backend."
            )
        client = self._get_client()
        kwargs = self._build_cloud_kwargs(
            provider, messages, model, tools, reasoning_effort, max_tokens, tool_choice, temperature
        )
        resp = client.chat.completions.create(**kwargs)
        return self._normalize_cloud_response(provider, resp.model_dump())

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "anthropic/claude-sonnet-4.6",
        max_tokens: int = 2048,
        reasoning_effort: str = "none",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} — for URL images
                - {"base64": "<b64>", "mime": "image/png"} — for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            reasoning_effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from env. LLM switches via tool if needed."""
        return os.environ.get("OUROBOROS_MODEL", "anthropic/claude-opus-4.6")

    def available_models(self) -> List[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = os.environ.get("OUROBOROS_MODEL", "anthropic/claude-opus-4.6")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
