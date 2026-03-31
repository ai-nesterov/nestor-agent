import os
import pathlib
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[1]


class TestProviderSourceGuards(unittest.TestCase):
    def test_safety_uses_runtime_cloud_provider_for_usage_events(self):
        source = (REPO / "ouroboros/safety.py").read_text(encoding="utf-8")
        self.assertIn('client.cloud_provider()', source)

    def test_consciousness_uses_runtime_cloud_provider_for_usage_events(self):
        source = (REPO / "ouroboros/consciousness.py").read_text(encoding="utf-8")
        self.assertIn('self._llm.cloud_provider()', source)

    def test_context_budget_drift_check_is_provider_aware(self):
        source = (REPO / "ouroboros/context.py").read_text(encoding="utf-8")
        self.assertIn('get_cloud_provider()', source)
