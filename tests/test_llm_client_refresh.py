import os
import sys
import types
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeOpenAI:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).created.append(self)


class _FakeAsyncOpenAI(_FakeOpenAI):
    pass


class TestLlmClientRefresh(unittest.TestCase):
    def setUp(self):
        _FakeOpenAI.created.clear()
        _FakeAsyncOpenAI.created.clear()

    def test_runtime_client_refreshes_when_env_key_changes(self):
        from ouroboros.llm import LLMClient

        fake_openai = types.SimpleNamespace(OpenAI=_FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_openai}):
            # Patch get_cloud_provider so both calls use openrouter regardless of
            # the real LLM_PROVIDER env var (which may be "minimax" in CI/dev).
            with patch("ouroboros.llm.get_cloud_provider", return_value="openrouter"):
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                    client = LLMClient()
                    first = client._get_client()

                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-new-key"}, clear=False):
                    second = client._get_client()

        self.assertIsNot(first, second)
        self.assertEqual(len(_FakeOpenAI.created), 2)
        self.assertEqual(_FakeOpenAI.created[0].kwargs["api_key"], "")
        self.assertEqual(_FakeOpenAI.created[1].kwargs["api_key"], "sk-or-new-key")

    def test_explicit_api_key_does_not_track_env_changes(self):
        from ouroboros.llm import LLMClient

        fake_openai = types.SimpleNamespace(OpenAI=_FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_openai}):
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
                client = LLMClient(api_key="explicit-key")
                first = client._get_client()

            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-new-key"}, clear=False):
                second = client._get_client()

        self.assertIs(first, second)
        self.assertEqual(len(_FakeOpenAI.created), 1)
        self.assertEqual(_FakeOpenAI.created[0].kwargs["api_key"], "explicit-key")

    def test_runtime_client_refreshes_when_openrouter_base_url_changes(self):
        from ouroboros.llm import LLMClient

        fake_openai = types.SimpleNamespace(OpenAI=_FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_openai}):
            # Patch get_cloud_provider so both calls use openrouter regardless of
            # the real LLM_PROVIDER env var (which may be "minimax" in CI/dev).
            with patch("ouroboros.llm.get_cloud_provider", return_value="openrouter"):
                with patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "sk-or-key",
                        "OPENROUTER_BASE_URL": "https://or-a.example/api/v1",
                    },
                    clear=False,
                ):
                    client = LLMClient()
                    first = client._get_client()

                with patch.dict(
                    os.environ,
                    {"OPENROUTER_API_KEY": "sk-or-key", "OPENROUTER_BASE_URL": "https://or-b.example/api/v1"},
                    clear=False,
                ):
                    second = client._get_client()

        self.assertIsNot(first, second)
        self.assertEqual(_FakeOpenAI.created[0].kwargs["base_url"], "https://or-a.example/api/v1")
        self.assertEqual(_FakeOpenAI.created[1].kwargs["base_url"], "https://or-b.example/api/v1")

    def test_runtime_client_uses_minimax_provider_selection(self):
        from ouroboros.llm import LLMClient

        fake_openai = types.SimpleNamespace(OpenAI=_FakeOpenAI, AsyncOpenAI=_FakeAsyncOpenAI)
        with patch.dict(sys.modules, {"openai": fake_openai}):
            with patch.dict(
                os.environ,
                {
                    "LLM_PROVIDER": "minimax",
                    "MINIMAX_API_KEY": "minimax-key",
                    "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
                },
                clear=False,
            ):
                client = LLMClient()
                instance = client._get_client()

        self.assertIsNotNone(instance)
        self.assertEqual(len(_FakeOpenAI.created), 1)
        self.assertEqual(_FakeOpenAI.created[0].kwargs["api_key"], "minimax-key")
        self.assertEqual(_FakeOpenAI.created[0].kwargs["base_url"], "https://api.minimax.io/v1")
        self.assertNotIn("default_headers", _FakeOpenAI.created[0].kwargs)

    def test_runtime_client_refreshes_when_provider_changes(self):
        from ouroboros.llm import LLMClient

        fake_openai = types.SimpleNamespace(OpenAI=_FakeOpenAI, AsyncOpenAI=_FakeAsyncOpenAI)
        with patch.dict(sys.modules, {"openai": fake_openai}):
            with patch.dict(
                os.environ,
                {
                    "LLM_PROVIDER": "openrouter",
                    "OPENROUTER_API_KEY": "sk-or-key",
                    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
                },
                clear=False,
            ):
                client = LLMClient()
                first = client._get_client()

            with patch.dict(
                os.environ,
                {
                    "LLM_PROVIDER": "minimax",
                    "MINIMAX_API_KEY": "minimax-key",
                    "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
                },
                clear=False,
            ):
                second = client._get_client()

        self.assertIsNot(first, second)
        self.assertEqual(_FakeOpenAI.created[0].kwargs["api_key"], "sk-or-key")
        self.assertEqual(_FakeOpenAI.created[1].kwargs["api_key"], "minimax-key")

    def test_local_chat_uses_port_fallback_without_auth_header(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

        class _FakeRequests:
            last_url = None
            last_headers = None

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.last_url = url
                _FakeRequests.last_headers = headers or {}
                return _FakeResponse()

        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {"LOCAL_MODEL_PORT": "9001", "LOCAL_MODEL_BASE_URL": "", "LOCAL_MODEL_API_KEY": ""},
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[{"role": "user", "content": "hi"}],
                    model="Qwen/Qwen3.5-27B",
                    tools=None,
                    max_tokens=64,
                    tool_choice="auto",
                )

        self.assertEqual(_FakeRequests.last_url, "http://127.0.0.1:9001/v1/chat/completions")
        self.assertNotIn("Authorization", _FakeRequests.last_headers)

    def test_local_chat_uses_base_url_and_bearer_when_api_key_set(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

        class _FakeRequests:
            last_url = None
            last_headers = None

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.last_url = url
                _FakeRequests.last_headers = headers or {}
                return _FakeResponse()

        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {
                    "LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1",
                    "LOCAL_MODEL_API_KEY": "local-secret",
                },
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[{"role": "user", "content": "hi"}],
                    model="Qwen/Qwen3.5-27B",
                    tools=None,
                    max_tokens=64,
                    tool_choice="auto",
                )

        self.assertEqual(_FakeRequests.last_url, "http://localhost:1234/v1/chat/completions")
        self.assertEqual(_FakeRequests.last_headers.get("Authorization"), "Bearer local-secret")

    def test_local_chat_falls_back_when_requested_model_not_found(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class _FakeRequests:
            post_models = []

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.post_models.append((url, (json or {}).get("model")))
                model = (json or {}).get("model")
                if model == "google/gemini-3-flash-preview":
                    return _FakeResponse(
                        404,
                        {
                            "type": "model_not_found",
                            "message": "Model 'google/gemini-3-flash-preview' not found",
                        },
                    )
                return _FakeResponse(
                    200,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )

            @staticmethod
            def get(url, headers=None, timeout=None):
                return _FakeResponse(
                    200,
                    {
                        "data": [
                            {"id": "Qwen/Qwen3.5-27B"},
                            {"id": "Qwen/Qwen3-Coder-Next"},
                        ]
                    },
                )

        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {
                    "LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1",
                    "OUROBOROS_MODEL": "Qwen/Qwen3.5-27B",
                    "LOCAL_MODEL_MAIN": "Qwen/Qwen3-Coder-Next",
                },
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[{"role": "user", "content": "hi"}],
                    model="google/gemini-3-flash-preview",
                    tools=None,
                    max_tokens=64,
                    tool_choice="auto",
                )

        self.assertEqual(
            _FakeRequests.post_models,
            [
                ("http://localhost:1234/v1/chat/completions", "google/gemini-3-flash-preview"),
                ("http://localhost:1234/v1/chat/completions", "Qwen/Qwen3-Coder-Next"),
            ],
        )

    def test_local_chat_falls_back_on_400_model_not_found_payload(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class _FakeRequests:
            post_models = []

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.post_models.append((url, (json or {}).get("model")))
                model = (json or {}).get("model")
                if model == "google/gemini-3-flash-preview":
                    return _FakeResponse(
                        400,
                        {
                            "type": "model_not_found",
                            "message": "Model 'google/gemini-3-flash-preview' not found",
                        },
                    )
                return _FakeResponse(
                    200,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )

            @staticmethod
            def get(url, headers=None, timeout=None):
                return _FakeResponse(
                    200,
                    {
                        "data": [
                            {"id": "Qwen/Qwen3.5-27B"},
                            {"id": "Qwen/Qwen3-Coder-Next"},
                        ]
                    },
                )

        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {
                    "LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1",
                    "OUROBOROS_MODEL": "Qwen/Qwen3.5-27B",
                },
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[{"role": "user", "content": "hi"}],
                    model="google/gemini-3-flash-preview",
                    tools=None,
                    max_tokens=64,
                    tool_choice="auto",
                )

        self.assertEqual(
            _FakeRequests.post_models,
            [
                ("http://localhost:1234/v1/chat/completions", "google/gemini-3-flash-preview"),
                ("http://localhost:1234/v1/chat/completions", "Qwen/Qwen3.5-27B"),
            ],
        )

    def test_local_chat_retries_with_shrunk_messages_on_context_error(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class _FakeRequests:
            calls = []

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.calls.append(dict(json or {}))
                if len(_FakeRequests.calls) == 1:
                    return _FakeResponse(
                        400,
                        {
                            "error": {
                                "code": "context_length_exceeded",
                                "message": (
                                    "This model's maximum context length is 8192 tokens. "
                                    "However, you requested 12000 tokens (10000 in the messages, "
                                    "2000 in the completion)."
                                ),
                            }
                        },
                    )
                return _FakeResponse(
                    200,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )

        large_system = "A" * 12000
        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {"LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1"},
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[
                        {"role": "system", "content": large_system},
                        {"role": "user", "content": "hi"},
                    ],
                    model="Qwen/Qwen3.5-27B",
                    tools=None,
                    max_tokens=2048,
                    tool_choice="auto",
                )

        self.assertEqual(len(_FakeRequests.calls), 2)
        first_system = _FakeRequests.calls[0]["messages"][0]["content"]
        second_system = _FakeRequests.calls[1]["messages"][0]["content"]
        self.assertGreater(len(first_system), len(second_system))
        self.assertIn("[Context truncated to fit model window]", second_system)

    def test_local_chat_retries_without_tools_when_backend_rejects_tool_payload(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return self._payload

        class _FakeRequests:
            calls = []

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.calls.append(dict(json or {}))
                if len(_FakeRequests.calls) == 1:
                    return _FakeResponse(
                        400,
                        {"error": {"message": "tools are not supported for this model"}},
                    )
                return _FakeResponse(
                    200,
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )

        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {"LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1"},
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[{"role": "user", "content": "hi"}],
                    model="Qwen/Qwen3.5-27B",
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "echo",
                                "description": "Echo text",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            },
                        }
                    ],
                    max_tokens=64,
                    tool_choice="auto",
                )

        self.assertEqual(len(_FakeRequests.calls), 2)
        self.assertIn("tools", _FakeRequests.calls[0])
        self.assertNotIn("tools", _FakeRequests.calls[1])
        self.assertNotIn("tool_choice", _FakeRequests.calls[1])

    def test_local_chat_reorders_late_system_messages_to_front(self):
        from ouroboros.llm import LLMClient

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

        class _FakeRequests:
            last_payload = None

            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                _FakeRequests.last_payload = dict(json or {})
                return _FakeResponse()

        with patch.dict(sys.modules, {"requests": _FakeRequests}):
            with patch.dict(
                os.environ,
                {"LOCAL_MODEL_BASE_URL": "http://localhost:1234/v1"},
                clear=False,
            ):
                client = LLMClient()
                client._chat_local(
                    messages=[
                        {"role": "system", "content": "global"},
                        {"role": "user", "content": "u1"},
                        {"role": "assistant", "content": "a1"},
                        {"role": "system", "content": "late-system"},
                        {"role": "user", "content": "u2"},
                    ],
                    model="Qwen/Qwen3.5-27B",
                    tools=None,
                    max_tokens=64,
                    tool_choice="auto",
                )

        payload_messages = (_FakeRequests.last_payload or {}).get("messages", [])
        roles = [m.get("role") for m in payload_messages]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertIn("global", payload_messages[0].get("content", ""))
        self.assertIn("late-system", payload_messages[0].get("content", ""))

    def test_available_models_uses_local_lane_model_ids(self):
        from ouroboros.llm import LLMClient

        with patch.dict(
            os.environ,
            {
                "OUROBOROS_MODEL": "MiniMax-M2.5",
                "OUROBOROS_MODEL_CODE": "MiniMax-M2.5",
                "OUROBOROS_MODEL_LIGHT": "MiniMax-M2.1-highspeed",
                "OUROBOROS_MODEL_FALLBACK": "MiniMax-M2.1",
                "LOCAL_MODEL_MAIN": "Qwen/Qwen3.5-27B",
                "LOCAL_MODEL_CODE": "Qwen/Qwen3-Coder-Next",
                "LOCAL_MODEL_LIGHT": "Qwen/Qwen3.5-27B",
                "LOCAL_MODEL_FALLBACK": "Qwen/Qwen3.5-27B",
                "USE_LOCAL_MAIN": "True",
                "USE_LOCAL_CODE": "True",
                "USE_LOCAL_LIGHT": "False",
                "USE_LOCAL_FALLBACK": "True",
                "MINIMAX_API_KEY": "minimax-key",
                "LLM_PROVIDER": "minimax",
            },
            clear=False,
        ):
            client = LLMClient()
            self.assertEqual(
                client.available_models(),
                [
                    "Qwen/Qwen3.5-27B",
                    "Qwen/Qwen3-Coder-Next",
                    "MiniMax-M2.1-highspeed",
                ],
            )

    def test_postprocess_response_message_extracts_think_reasoning(self):
        from ouroboros.llm import LLMClient

        client = LLMClient()
        msg = client._postprocess_response_message(
            "minimax",
            {"role": "assistant", "content": "<think>hidden chain</think>\n```json\n{\"ok\": true}\n```"},
        )

        self.assertEqual(msg["content"], "```json\n{\"ok\": true}\n```")
        self.assertEqual(msg["reasoning"], "hidden chain")
        self.assertEqual(msg["raw_content"], "<think>hidden chain</think>\n```json\n{\"ok\": true}\n```")
        self.assertEqual(msg["provider"], "minimax")

    def test_postprocess_response_message_handles_text_blocks(self):
        from ouroboros.llm import LLMClient

        client = LLMClient()
        msg = client._postprocess_response_message(
            "minimax",
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "<think>step one</think>\nVisible"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                ],
            },
        )

        self.assertEqual(msg["content"][0]["text"], "Visible")
        self.assertEqual(msg["reasoning"], "step one")
        self.assertEqual(msg["provider"], "minimax")
        self.assertIn("raw_content", msg)
