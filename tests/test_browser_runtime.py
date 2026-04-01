import pathlib
import tempfile
import unittest

from ouroboros.tools.browser import _candidate_python_executables, _venv_python_path, _venv_site_packages
from ouroboros.tools.registry import ToolContext


class TestBrowserRuntimeHelpers(unittest.TestCase):
    def test_detects_repo_local_venv_python_first(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        env_dir = tmp / "nestor_agent_env" / "bin"
        env_dir.mkdir(parents=True)
        py = env_dir / "python"
        py.write_text("", encoding="utf-8")

        ctx = ToolContext(repo_dir=tmp, drive_root=tmp)
        candidates = _candidate_python_executables(ctx)

        self.assertEqual(candidates[0], str(py))

    def test_venv_site_packages_finds_posix_path(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        site = tmp / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)

        self.assertIn(site, _venv_site_packages(tmp))

    def test_venv_python_path_matches_posix_layout(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        expected = tmp / "bin" / "python"
        self.assertEqual(_venv_python_path(tmp), expected)
