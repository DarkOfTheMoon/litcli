"""A small on-disk cache for metadata reads.

Searching the same thing twice, or paging through a subject, otherwise re-requests every
source and walks straight into the same rate limits. Only successful ``GET`` JSON/text
bodies are kept — never writes, never files — for a day, and the whole thing is off with
``--no-cache`` or ``LITCLI_NO_CACHE=1``.

The cache is a nicety: every failure path (missing directory, corrupt entry, unwritable
disk) degrades to a request.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .paths import cache_dir

#: Seconds an entry stays usable.
TTL = 24 * 60 * 60
#: Entries before a prune keeps the cache from growing without bound.
MAX_ENTRIES = 4000

_DISABLED = os.environ.get("LITCLI_NO_CACHE", "").lower() in {"1", "true", "yes", "on"}

#: Hit/miss counters since the last :func:`reset_stats`. A survey hop resets them at its start
#: and refunds its hits in full — a cached answer cost no request, so it must cost no quota.
#: ``by`` counts the same split per source, and counts *requests* rather than lookups: the site
#: quota is per source, so the spend has to be attributable to one (and ``note`` is called on
#: the network path too, where a disabled cache never reaches :func:`get`).
_STATS: dict = {"hits": 0, "misses": 0, "by": {}}


def enabled() -> bool:
    return not _DISABLED


def stats() -> dict:
    """The counters since the last reset (a copy, per-source block included)."""
    return {**_STATS, "by": {name: dict(slot) for name, slot in _STATS["by"].items()}}


def reset_stats() -> None:
    """Zero every counter."""
    _STATS["hits"] = 0
    _STATS["misses"] = 0
    _STATS["by"] = {}


def note(name: str, hit: bool) -> None:
    """Count one request outcome for ``name`` (a cache hit cost the site nothing)."""
    slot = _STATS["by"].setdefault(name, {"hits": 0, "misses": 0})
    slot["hits" if hit else "misses"] += 1


def disable() -> None:
    global _DISABLED
    _DISABLED = True


def _path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir() / digest[:2] / digest


def get(key: str) -> str | None:
    """The cached body for ``key``, or ``None`` when absent, stale or unreadable."""
    if _DISABLED:
        return None
    body = _read(key)
    _STATS["hits" if body is not None else "misses"] += 1
    return body


def _read(key: str) -> str | None:
    try:
        with _path(key).open(encoding="utf-8") as f:
            entry = json.load(f)
        if not isinstance(entry, dict):
            return None
        stored = entry.get("stored", 0)
        if not isinstance(stored, (int, float)) or time.time() - stored > TTL:
            return None
        body = entry.get("body")
        return body if isinstance(body, str) else None
    except (OSError, ValueError):
        return None


def put(key: str, body: str) -> None:
    """Store ``body`` under ``key``; a failure here never fails the request."""
    if _DISABLED or body is None:
        return
    _STATS["misses"] += 1
    path = _path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A per-writer temp name: openalex fans out from several threads, and a shared `.tmp`
        # lets two writers truncate each other mid-dump.
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"stored": time.time(), "body": body}, f)
        tmp.replace(path)
    except OSError:
        return
    _maybe_prune()


def _maybe_prune() -> None:
    """Keep the cache bounded: drop expired entries, then the oldest until under the cap.

    Called from the write path only. Stale-only pruning was not enough — a day of fresh entries
    never shrank the cache, so ``MAX_ENTRIES`` was a cap in name only.
    """
    root = cache_dir()
    try:
        files = [(p, p.stat().st_mtime) for p in root.rglob("*") if p.is_file()]
    except OSError:
        return
    if len(files) <= MAX_ENTRIES:
        return
    now = time.time()
    kept = []
    for p, mtime in files:
        if now - mtime > TTL:
            with contextlib.suppress(OSError):
                p.unlink()
        else:
            kept.append((p, mtime))
    for p, _mtime in sorted(kept, key=lambda f: f[1])[:len(kept) - MAX_ENTRIES]:
        try:
            p.unlink()
        except OSError:
            continue


def clear() -> int:
    """Remove every entry; returns how many were removed."""
    root = cache_dir()
    removed = 0
    try:
        for p in root.rglob("*"):
            if p.is_file():
                p.unlink()
                removed += 1
    except OSError:
        pass
    return removed
