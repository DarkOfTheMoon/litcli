"""A small on-disk cache for metadata reads.

Searching the same thing twice, or paging through a subject, otherwise re-requests every
source and walks straight into the same rate limits. Only successful ``GET`` JSON/text
bodies are kept — never writes, never files — for a day, and the whole thing is off with
``--no-cache`` or ``LITCLI_NO_CACHE=1``.

The cache is a nicety: every failure path (missing directory, corrupt entry, unwritable
disk) degrades to a request.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .paths import cache_dir

#: Seconds an entry stays usable.
TTL = 24 * 60 * 60
#: Entries before a prune keeps the cache from growing without bound.
MAX_ENTRIES = 4000

_DISABLED = os.environ.get("LITCLI_NO_CACHE", "").lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return not _DISABLED


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
    try:
        with _path(key).open(encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry.get("stored", 0) > TTL:
            return None
        return entry.get("body")
    except (OSError, ValueError):
        return None


def put(key: str, body: str) -> None:
    """Store ``body`` under ``key``; a failure here never fails the request."""
    if _DISABLED or body is None:
        return
    path = _path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"stored": time.time(), "body": body}, f)
        tmp.replace(path)
    except OSError:
        return
    _maybe_prune()


def _maybe_prune() -> None:
    """Drop old entries when the cache is large; called from the write path only."""
    root = cache_dir()
    try:
        files = [p for p in root.rglob("*") if p.is_file()]
    except OSError:
        return
    if len(files) <= MAX_ENTRIES:
        return
    now = time.time()
    for p in files:
        try:
            if now - p.stat().st_mtime > TTL:
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
