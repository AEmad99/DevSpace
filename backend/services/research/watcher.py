"""Debounced file-system watcher for KnowledgeBaseSource (issue #2 / M4).

Watches the member folders of a Knowledge Base and triggers a re-warmup
when files change. Multiple rapid events are collapsed into a single
debounced call so editing a few files at once doesn't pin a CPU.

Design:
  - One `Observer` per KB session, started on `attach()`.
  - Each member folder gets its own `DebouncedHandler`.
  - The handler uses a `threading.Timer` to coalesce events that arrive
    within `debounce_seconds` (default 5s). The actual re-warmup is
    scheduled on the research event loop via `run_coroutine_threadsafe`.
  - Watchdog is an OPTIONAL dependency. When it's not installed, the
    attach() call returns a no-op observer and logs a single INFO line.
    The user still gets the KB, just without live auto-reindex.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class _NullObserver:
    """Stand-in returned when watchdog isn't installed."""

    def schedule(self, *args, **kwargs):  # pragma: no cover - never called
        pass

    def start(self):  # pragma: no cover
        pass

    def stop(self):  # pragma: no cover
        pass

    def join(self, timeout=None):  # pragma: no cover
        pass

    def is_alive(self) -> bool:  # pragma: no cover
        return False


class _DebouncedHandler:
    """Coalesces filesystem events within `debounce_seconds` into one callback."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        callback,
        *,
        debounce_seconds: float = 5.0,
    ):
        self._loop = loop
        self._cb = callback
        self._delay = debounce_seconds
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _trigger(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        # Schedule the async callback on the captured event loop.
        try:
            fut = asyncio.run_coroutine_threadsafe(self._cb(), self._loop)
            # Don't block the watchdog thread waiting for warmup.
            fut.add_done_callback(self._log_errors)
        except Exception as e:
            logger.warning(f"watcher: failed to schedule reindex: {e}")

    def _log_errors(self, fut):
        try:
            fut.result()
        except Exception as e:
            logger.warning(f"watcher: reindex callback raised: {e}")

    # watchdog FileSystemEventHandler API
    def on_any_event(self, event):  # pragma: no cover - exercised only with watchdog
        # Ignore directory events and permission-only events.
        if event.is_directory:
            return
        self._trigger()


def attach_watcher(
    kb_source,
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    debounce_seconds: float = 5.0,
):
    """Attach a debounced watchdog observer to every member folder of `kb_source`.

    Returns the observer (or a null observer if watchdog is missing).
    Callers MUST call `observer.stop(); observer.join()` when the research
    session ends to release the threads.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("watcher.attach_watcher: no running event loop; "
                           "skipping (auto-reindex disabled for this session)")
            return _NullObserver()

    try:
        from watchdog.observers import Observer  # type: ignore
        from watchdog.events import FileSystemEventHandler  # type: ignore
    except ImportError:
        logger.info("watchdog not installed; KB auto-reindex disabled "
                    "(pip install watchdog to enable).")
        return _NullObserver()

    # Concrete subclass to bind our handler class to watchdog's API.
    class _Handler(FileSystemEventHandler):
        def __init__(self_inner):
            super().__init__()
            # The actual work happens via on_any_event which calls _trigger.
            inner = _DebouncedHandler(
                loop, kb_source.warmup, debounce_seconds=debounce_seconds
            )
            self_inner._inner = inner

        def on_any_event(self_inner, event):
            self_inner._inner.on_any_event(event)

    obs = Observer()
    for member in kb_source._member_sources:
        try:
            obs.schedule(_Handler(), str(member.root), recursive=True)
        except Exception as e:
            logger.warning(f"watcher: could not watch {member.root}: {e}")
    obs.daemon = True
    obs.start()
    return obs


def detach_watcher(observer) -> None:
    """Stop + join a previously-attached observer. Safe on the null observer."""
    if observer is None:
        return
    try:
        observer.stop()
        observer.join(timeout=2.0)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"detach_watcher: {e}")
