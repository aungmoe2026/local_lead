"""
JS-rendering fallback for contact_enricher.py, backed by crawl4ai (Playwright).

Many small-business sites (Wix/Squarespace/React) return a near-empty HTML
shell to a plain `requests.get()` — the real content only appears after
client-side JS runs. contact_enricher.py tries the cheap `requests` path
first for every site; this module is only invoked when that path looks like
it hit a JS shell (see contact_enricher._looks_js_rendered).

crawl4ai's AsyncWebCrawler is asyncio/Playwright-based, but the rest of this
app is sync (contact_enricher runs inside jobs.py's ThreadPoolExecutor). To
bridge that without starting a Chromium instance per enrichment thread (far
too heavy at ENRICHMENT_WORKERS concurrency), this module runs ONE shared
AsyncWebCrawler on ONE dedicated background thread with its own event loop,
and exposes a plain sync `render()` function that any worker thread can call.
Real render concurrency is capped independently via an asyncio.Semaphore.

If crawl4ai/Playwright isn't installed, or the browser binary is missing,
this fails once, logs it, and disables itself for the rest of the process —
callers always get None back rather than a raised exception, matching the
"optional, degrade gracefully" pattern used for Companies House.
"""

import asyncio
import threading
from typing import Optional

from config import JS_RENDER_ENABLED, JS_RENDER_MAX_CONCURRENCY, JS_RENDER_TIMEOUT_SECONDS

_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_crawler = None
_semaphore: Optional[asyncio.Semaphore] = None
_disabled = not JS_RENDER_ENABLED
_disabled_reason = "" if JS_RENDER_ENABLED else "disabled in config"


def _run_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


class _NullLogger:
    """crawl4ai's default AsyncLogger writes Rich console output that includes
    Unicode status icons (arrows/checkmarks) — on Windows terminals still
    using a legacy codepage (cp1252) that raises UnicodeEncodeError deep
    inside Rich, which crashes the whole arun() call. BrowserConfig's
    verbose=False doesn't suppress it (it's a separate logger object), so we
    swap in a logger that does nothing instead of trying to fix console
    encoding globally."""

    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def success(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def url_status(self, *a, **k): pass
    def error_status(self, *a, **k): pass


async def _init_crawler():
    global _crawler, _semaphore
    from crawl4ai import AsyncWebCrawler
    from crawl4ai.async_configs import BrowserConfig

    _semaphore = asyncio.Semaphore(JS_RENDER_MAX_CONCURRENCY)
    _crawler = AsyncWebCrawler(
        config=BrowserConfig(headless=True, verbose=False), logger=_NullLogger()
    )
    await _crawler.start()


def _ensure_started() -> bool:
    """Lazily starts the background loop + shared crawler on first use.
    Returns False (and permanently disables JS rendering) if setup fails."""
    global _loop, _disabled, _disabled_reason

    if _disabled:
        return False
    if _crawler is not None:
        return True

    with _lock:
        if _disabled:
            return False
        if _crawler is not None:
            return True

        try:
            _loop = asyncio.new_event_loop()
            thread = threading.Thread(target=_run_loop, args=(_loop,), daemon=True)
            thread.start()
            future = asyncio.run_coroutine_threadsafe(_init_crawler(), _loop)
            future.result(timeout=60)
            return True
        except Exception as exc:  # noqa: BLE001 - any setup failure just disables the feature
            _disabled = True
            _disabled_reason = str(exc)
            print(
                f"[js_render] Disabled for this run — crawl4ai/Playwright setup failed "
                f"({exc}). Falling back to plain HTTP fetches only. "
                f"Run 'python -m playwright install chromium' to enable it."
            )
            return False


async def _render_async(url: str, timeout_s: float) -> Optional[str]:
    from crawl4ai.async_configs import CrawlerRunConfig

    async with _semaphore:
        config = CrawlerRunConfig(page_timeout=int(timeout_s * 1000))
        result = await _crawler.arun(url=url, config=config)
        if result and result.success and result.html:
            return result.html
        return None


def render(url: str, timeout_s: float = JS_RENDER_TIMEOUT_SECONDS) -> Optional[str]:
    """Render `url` in a real (headless) browser and return the resulting HTML,
    or None if JS rendering is unavailable, times out, or the page failed to load."""
    if not _ensure_started():
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(_render_async(url, timeout_s), _loop)
        return future.result(timeout=timeout_s + 10)
    except Exception:  # noqa: BLE001 - a single page failing to render must not crash the caller
        return None
