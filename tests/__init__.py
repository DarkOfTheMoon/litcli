"""Test package setup: keep the HTTP response cache out of the real user directory.

Every stub test would otherwise share (and be served by) whatever is already cached for the
same URL, so the suite gets its own throwaway ``XDG_CACHE_HOME``.
"""
import os
import tempfile

os.environ.setdefault("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="litcli-cache-"))
