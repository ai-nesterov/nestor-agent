import types
import unittest
from unittest.mock import patch

from ouroboros.tools.browser import _browser_action


class _FakePage:
    def __init__(self):
        self.calls = []

    def goto(self, url, timeout=None, wait_until=None):
        self.calls.append(("goto", url, timeout, wait_until))

    def press(self, selector, value, timeout=None):
        self.calls.append(("press", selector, value, timeout))

    def wait_for_function(self, script, value, timeout=None):
        self.calls.append(("wait_for_function", script, value, timeout))

    def evaluate(self, script):
        self.calls.append(("evaluate", script))
        if "querySelectorAll('a[href]')" in script:
            return [
                {"text": "Example", "url": "https://example.com"},
                {"text": "Docs", "url": "https://example.com/docs"},
            ]
        if "querySelectorAll('input, textarea, select, button')" in script:
            return [
                {"tag": "input", "type": "text", "name": "q", "id": "search", "placeholder": "Search", "value": "", "text": ""},
            ]
        return "ok"


class TestBrowserActions(unittest.TestCase):
    def _make_ctx(self):
        return types.SimpleNamespace(browser_state=types.SimpleNamespace(last_screenshot_b64=None))

    def test_navigate_action(self):
        page = _FakePage()
        with patch("ouroboros.tools.browser._ensure_browser", return_value=page):
            result = _browser_action(self._make_ctx(), "navigate", value="https://example.com", timeout=1234)
        self.assertEqual(result, "Navigated to: https://example.com")
        self.assertEqual(page.calls[0], ("goto", "https://example.com", 1234, "domcontentloaded"))

    def test_press_action(self):
        page = _FakePage()
        with patch("ouroboros.tools.browser._ensure_browser", return_value=page):
            result = _browser_action(self._make_ctx(), "press", selector="#search", value="Enter", timeout=500)
        self.assertEqual(result, "Pressed Enter on #search")
        self.assertEqual(page.calls[0], ("press", "#search", "Enter", 500))

    def test_wait_for_text_action(self):
        page = _FakePage()
        with patch("ouroboros.tools.browser._ensure_browser", return_value=page):
            result = _browser_action(self._make_ctx(), "wait_for_text", value="Loaded", timeout=900)
        self.assertEqual(result, "Found text: Loaded")
        self.assertEqual(page.calls[0][0], "wait_for_function")
        self.assertEqual(page.calls[0][2], "Loaded")

    def test_extract_links_action(self):
        page = _FakePage()
        with patch("ouroboros.tools.browser._ensure_browser", return_value=page):
            result = _browser_action(self._make_ctx(), "extract_links")
        self.assertIn("https://example.com", result)
        self.assertIn("Docs", result)

    def test_extract_form_fields_action(self):
        page = _FakePage()
        with patch("ouroboros.tools.browser._ensure_browser", return_value=page):
            result = _browser_action(self._make_ctx(), "extract_form_fields")
        self.assertIn("'name': 'q'", result)
