"""
Browser automation tools via Playwright (sync API).

Provides browse_page (open URL, get content/screenshot)
and browser_action (click, fill, evaluate JS on current page).

Each BrowserState (in ToolContext) fully owns its Playwright lifecycle:
no module-level singletons, no cross-context sharing. Thread affinity
is tracked per-BrowserState via _thread_id.
"""

from __future__ import annotations

import base64
import importlib
import logging
import os
import pathlib
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_playwright_ready = False
_playwright_python: Optional[str] = None
_HAS_STEALTH = False


def _venv_python_path(env_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _venv_site_packages(env_dir: pathlib.Path) -> List[pathlib.Path]:
    if os.name == "nt":
        path = env_dir / "Lib" / "site-packages"
        return [path] if path.exists() else []

    lib_dir = env_dir / "lib"
    if not lib_dir.exists():
        return []
    return sorted(p for p in lib_dir.glob("python*/site-packages") if p.exists())


def _candidate_env_dirs(ctx: ToolContext) -> List[pathlib.Path]:
    candidates = [
        ctx.repo_dir / "nestor_agent_env",
        ctx.repo_dir / ".venv",
        ctx.repo_dir / "venv",
    ]
    return [p.resolve() for p in candidates if p.exists()]


def _candidate_python_executables(ctx: ToolContext) -> List[str]:
    candidates = []
    for env_dir in _candidate_env_dirs(ctx):
        py = _venv_python_path(env_dir)
        if py.exists():
            candidates.append(str(py))
    candidates.append(sys.executable)
    return candidates


def _ensure_local_playwright_on_syspath(ctx: ToolContext) -> bool:
    for env_dir in _candidate_env_dirs(ctx):
        for site_packages in _venv_site_packages(env_dir):
            if str(site_packages) not in sys.path:
                sys.path.insert(0, str(site_packages))
            try:
                importlib.import_module("playwright")
                return True
            except ImportError:
                continue
    return False


def _ensure_stealth_imported() -> None:
    global _HAS_STEALTH
    try:
        importlib.import_module("playwright_stealth")
        _HAS_STEALTH = True
    except ImportError:
        _HAS_STEALTH = False


def _ensure_playwright_module(ctx: ToolContext) -> None:
    try:
        importlib.import_module("playwright")
    except ImportError:
        if _ensure_local_playwright_on_syspath(ctx):
            _ensure_stealth_imported()
            return
        raise
    _ensure_stealth_imported()


def _detect_playwright_python(ctx: ToolContext) -> str:
    global _playwright_python
    if _playwright_python:
        return _playwright_python
    for candidate in _candidate_python_executables(ctx):
        try:
            res = subprocess.run(
                [candidate, "-c", "import playwright; print('ok')"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if res.returncode == 0 and "ok" in (res.stdout or ""):
                _playwright_python = candidate
                return candidate
        except Exception:
            continue
    _playwright_python = sys.executable
    return _playwright_python


def _run_playwright_python(cmd: List[str]) -> None:
    subprocess.check_call(cmd)


def _ensure_playwright_installed(ctx: ToolContext):
    """Ensure Playwright is importable in-process and Chromium is installed in a local runtime."""
    global _playwright_ready
    if _playwright_ready:
        return

    runtime_python = _detect_playwright_python(ctx)

    try:
        _ensure_playwright_module(ctx)
    except ImportError:
        if getattr(sys, 'frozen', False):
            raise RuntimeError(
                "Browser tools require Playwright, which is not bundled. "
                "Install manually: pip3 install playwright && python3 -m playwright install chromium"
            )
        log.info("Playwright not found, installing into %s...", runtime_python)
        try:
            _run_playwright_python([runtime_python, "-m", "pip", "install", "playwright", "playwright-stealth"])
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Browser tools require Playwright, but automatic installation failed. "
                "Create a virtualenv or install Playwright manually."
            ) from e
        _ensure_playwright_module(ctx)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            executable = pathlib.Path(pw.chromium.executable_path)
        if not executable.exists():
            raise RuntimeError(f"Chromium executable missing at {executable}")
        log.info("Playwright chromium binary found at %s", executable)
    except Exception:
        if getattr(sys, 'frozen', False):
            raise RuntimeError(
                "Playwright chromium binary not found. "
                "Install manually: python3 -m playwright install chromium"
            )
        log.info("Installing Playwright chromium binary via %s...", runtime_python)
        try:
            _run_playwright_python([runtime_python, "-m", "playwright", "install", "chromium"])
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Playwright is installed, but Chromium download failed. "
                "Check network access or install Chromium manually for browser tools."
            ) from e

    _playwright_ready = True


def _ensure_browser(ctx: ToolContext):
    """Create or reuse browser for this context. All Playwright state lives
    in ctx.browser_state — no module-level globals."""
    bs = ctx.browser_state
    current_thread_id = threading.get_ident()

    if bs._thread_id is not None and bs._thread_id != current_thread_id:
        log.info("Thread switch detected (old=%s, new=%s). Tearing down browser for this context.",
                 bs._thread_id, current_thread_id)
        cleanup_browser(ctx)

    if bs.browser is not None:
        try:
            if bs.browser.is_connected():
                return bs.page
        except Exception:
            log.debug("Browser connection check failed", exc_info=True)
        cleanup_browser(ctx)

    _ensure_playwright_installed(ctx)

    if bs.pw_instance is None:
        from playwright.sync_api import sync_playwright
        bs.pw_instance = sync_playwright().start()
        bs._thread_id = current_thread_id
        log.info("Created Playwright instance in thread %s", current_thread_id)

    bs.browser = bs.pw_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=site-per-process",
            "--window-size=1920,1080",
        ],
    )
    bs.page = bs.browser.new_page(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )

    if _HAS_STEALTH:
        from playwright_stealth import Stealth
        stealth = Stealth()
        stealth.apply_stealth_sync(bs.page)

    bs.page.set_default_timeout(30000)
    return bs.page


def cleanup_browser(ctx: ToolContext) -> None:
    """Full teardown: close page, browser, AND stop the Playwright instance.
    Called by agent.py in finally block and by recovery logic on errors."""
    bs = ctx.browser_state
    try:
        if bs.page is not None:
            bs.page.close()
    except Exception:
        log.debug("Failed to close browser page during cleanup", exc_info=True)
    try:
        if bs.browser is not None:
            bs.browser.close()
    except Exception:
        log.debug("Failed to close browser during cleanup", exc_info=True)
    try:
        if bs.pw_instance is not None:
            bs.pw_instance.stop()
    except Exception:
        log.debug("Failed to stop Playwright instance during cleanup", exc_info=True)
    bs.page = None
    bs.browser = None
    bs.pw_instance = None
    bs._thread_id = None


def _is_infrastructure_error(obj: Any) -> bool:
    """Detect browser infrastructure failure from either context state or an error object.

    Backward-compat: older tests call this with an exception and expect string-based
    detection of greenlet/thread/browser teardown failures.
    """
    if hasattr(obj, "browser_state"):
        bs = obj.browser_state
        if bs.browser is None or bs.pw_instance is None:
            return True
        try:
            if not bs.browser.is_connected():
                return True
        except Exception:
            return True
        if bs.page is not None:
            try:
                if bs.page.is_closed():
                    return True
            except Exception:
                return True
        return False

    msg = str(obj).lower()
    return any(token in msg for token in (
        "green thread",
        "different thread",
        "browser has been closed",
        "page has been closed",
        "connection closed",
    ))


_MARKDOWN_JS = """() => {
    const walk = (el) => {
        let out = '';
        for (const child of el.childNodes) {
            if (child.nodeType === 3) {
                const t = child.textContent.trim();
                if (t) out += t + ' ';
            } else if (child.nodeType === 1) {
                const tag = child.tagName;
                if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                if (['H1','H2','H3','H4','H5','H6'].includes(tag))
                    out += '\\n' + '#'.repeat(parseInt(tag[1])) + ' ';
                if (tag === 'P' || tag === 'DIV' || tag === 'BR') out += '\\n';
                if (tag === 'LI') out += '\\n- ';
                if (tag === 'A') out += '[';
                out += walk(child);
                if (tag === 'A') out += '](' + (child.href||'') + ')';
            }
        }
        return out;
    };
    return walk(document.body);
}"""


def _extract_page_output(page: Any, output: str, ctx: ToolContext) -> str:
    """Extract page content in the requested format."""
    if output == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        ctx.browser_state.last_screenshot_b64 = b64
        return (
            f"Screenshot captured ({len(b64)} bytes base64). "
            f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the user."
        )
    elif output == "html":
        html = page.content()
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    elif output == "markdown":
        text = page.evaluate(_MARKDOWN_JS)
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")
    else:  # text
        text = page.inner_text("body")
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")


def _extract_links(page: Any) -> str:
    links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
        text: (a.innerText || a.textContent || '').trim(),
        url: a.href || '',
    }))""")
    cleaned = []
    for item in links or []:
        url = str((item or {}).get("url") or "").strip()
        text = str((item or {}).get("text") or "").strip()
        if not url:
            continue
        cleaned.append({"text": text, "url": url})
        if len(cleaned) >= 50:
            break
    return str(cleaned)


def _extract_form_fields(page: Any) -> str:
    fields = page.evaluate("""() => Array.from(document.querySelectorAll('input, textarea, select, button')).map((el) => ({
        tag: el.tagName.toLowerCase(),
        type: (el.getAttribute('type') || '').toLowerCase(),
        name: el.getAttribute('name') || '',
        id: el.id || '',
        placeholder: el.getAttribute('placeholder') || '',
        value: 'value' in el ? String(el.value || '') : '',
        text: (el.innerText || el.textContent || '').trim(),
    }))""")
    return str((fields or [])[:100])


def _browse_page(ctx: ToolContext, url: str, output: str = "text",
                 wait_for: str = "", timeout: int = 30000,
                 viewport: str = "") -> str:
    try:
        page = _ensure_browser(ctx)
        if viewport:
            _apply_viewport(page, viewport)
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout)
        return _extract_page_output(page, output, ctx)
    except Exception as e:
        had_browser_state = any(
            getattr(ctx.browser_state, attr, None) is not None
            for attr in ("pw_instance", "browser", "page")
        )
        if had_browser_state and _is_infrastructure_error(ctx):
            log.warning("Browser infrastructure error: %s. Cleaning up and retrying...", e)
            cleanup_browser(ctx)
            page = _ensure_browser(ctx)
            if viewport:
                _apply_viewport(page, viewport)
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_for:
                page.wait_for_selector(wait_for, timeout=timeout)
            return _extract_page_output(page, output, ctx)
        raise


def _apply_viewport(page: Any, viewport: str) -> None:
    """Parse a 'WxH' string and resize the browser viewport."""
    try:
        parts = viewport.lower().split("x")
        w, h = int(parts[0]), int(parts[1])
        page.set_viewport_size({"width": max(320, w), "height": max(480, h)})
    except (ValueError, IndexError):
        log.warning("Invalid viewport '%s', expected WxH (e.g. '375x812')", viewport)


def _browser_action(ctx: ToolContext, action: str, selector: str = "",
                    value: str = "", timeout: int = 5000) -> str:
    def _do_action():
        page = _ensure_browser(ctx)

        if action == "click":
            if not selector:
                return "Error: selector required for click"
            page.click(selector, timeout=timeout)
            page.wait_for_timeout(500)
            return f"Clicked: {selector}"
        elif action == "fill":
            if not selector:
                return "Error: selector required for fill"
            page.fill(selector, value, timeout=timeout)
            return f"Filled {selector} with: {value}"
        elif action == "select":
            if not selector:
                return "Error: selector required for select"
            page.select_option(selector, value, timeout=timeout)
            return f"Selected {value} in {selector}"
        elif action == "navigate":
            if not value:
                return "Error: value (URL) required for navigate"
            page.goto(value, timeout=timeout, wait_until="domcontentloaded")
            return f"Navigated to: {value}"
        elif action == "press":
            if not selector:
                return "Error: selector required for press"
            if not value:
                return "Error: value (key) required for press"
            page.press(selector, value, timeout=timeout)
            return f"Pressed {value} on {selector}"
        elif action == "wait_for_text":
            if not value:
                return "Error: value (text) required for wait_for_text"
            page.wait_for_function(
                """(needle) => document.body && document.body.innerText.includes(needle)""",
                value,
                timeout=timeout,
            )
            return f"Found text: {value}"
        elif action == "screenshot":
            data = page.screenshot(type="png", full_page=False)
            b64 = base64.b64encode(data).decode()
            ctx.browser_state.last_screenshot_b64 = b64
            return (
                f"Screenshot captured ({len(b64)} bytes base64). "
                f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the user."
            )
        elif action == "evaluate":
            if not value:
                return "Error: value (JS code) required for evaluate"
            result = page.evaluate(value)
            out = str(result)
            return out[:20000] + ("... [truncated]" if len(out) > 20000 else "")
        elif action == "scroll":
            direction = value or "down"
            if direction == "down":
                page.evaluate("window.scrollBy(0, 600)")
            elif direction == "up":
                page.evaluate("window.scrollBy(0, -600)")
            elif direction == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return f"Scrolled {direction}"
        elif action == "extract_links":
            return _extract_links(page)
        elif action == "extract_form_fields":
            return _extract_form_fields(page)
        else:
            return (
                "Unknown action: "
                f"{action}. Use: click, fill, select, navigate, press, wait_for_text, "
                "screenshot, evaluate, scroll, extract_links, extract_form_fields"
            )

    try:
        return _do_action()
    except Exception as e:
        had_browser_state = any(
            getattr(ctx.browser_state, attr, None) is not None
            for attr in ("pw_instance", "browser", "page")
        )
        if had_browser_state and _is_infrastructure_error(ctx):
            log.warning("Browser infrastructure error: %s. Cleaning up and retrying...", e)
            cleanup_browser(ctx)
            return _do_action()
        raise


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": (
                    "Open a URL in headless browser. Returns page content as text, "
                    "html, markdown, or screenshot (base64 PNG). "
                    "Browser persists across calls within a task. "
                    "For screenshots: use send_photo tool to deliver the image to the user. "
                    "Use viewport to test mobile layouts (e.g. '375x812')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to open"},
                        "output": {
                            "type": "string",
                            "enum": ["text", "html", "markdown", "screenshot"],
                            "description": "Output format (default: text)",
                        },
                        "wait_for": {
                            "type": "string",
                            "description": "CSS selector to wait for before extraction",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Page load timeout in ms (default: 30000)",
                        },
                        "viewport": {
                            "type": "string",
                            "description": "Viewport size as WxH (e.g. '375x812' for mobile, '1920x1080' for desktop). Default: current viewport.",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=_browse_page,
            timeout_sec=180,
        ),
        ToolEntry(
            name="browser_action",
            schema={
                "name": "browser_action",
                "description": (
                    "Perform action on current browser page. Actions: "
                    "click (selector), fill (selector + value), select (selector + value), "
                    "navigate (value=url), press (selector + value=key), "
                    "wait_for_text (value=text), screenshot (base64 PNG), "
                    "evaluate (JS code in value), scroll (value: up/down/top/bottom), "
                    "extract_links, extract_form_fields."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "click", "fill", "select", "navigate", "press",
                                "wait_for_text", "screenshot", "evaluate", "scroll",
                                "extract_links", "extract_form_fields",
                            ],
                            "description": "Action to perform",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for click/fill/select/press",
                        },
                        "value": {
                            "type": "string",
                            "description": "Value for fill/select, URL for navigate, key for press, text for wait_for_text, JS for evaluate, direction for scroll",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Action timeout in ms (default: 5000)",
                        },
                    },
                    "required": ["action"],
                },
            },
            handler=_browser_action,
            timeout_sec=180,
        ),
    ]
