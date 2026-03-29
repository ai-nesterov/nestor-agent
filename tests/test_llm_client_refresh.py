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


class TestLlmClientRefresh(unittest.TestCase):
    def setUp(self):
        _FakeOpenAI.created.clear()

    def test_runtime_client_refreshes_when_env_key_changes(self):
        from ouroboros.llm import LLMClient

        fake_openai = types.SimpleNamespace(OpenAI=_FakeOpenAI)
        with patch.dict(sys.modules, {"openai": fake_openai}):
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
            with patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "sk-or-key", "OPENROUTER_BASE_URL": "https://or-a.example/api/v1"},
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
                    tools=None,
                    max_tokens=64,
                    tool_choice="auto",
                )

        self.assertEqual(_FakeRequests.last_url, "http://localhost:1234/v1/chat/completions")
        self.assertEqual(_FakeRequests.last_headers.get("Authorization"), "Bearer local-secret")
