"""PDF handling: download + text extraction via the system pdftotext CLI.

Backend-agnostic: Zotero attachments and arXiv direct links are both normalized to
"given bytes/path -> extract text".
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.parse

import httpx

from .transport import client

# Only http(s) sources may be downloaded; anything else (e.g. file://) could read local files
# if a metadata/OA source ever handed back a path URL, so it is rejected outright.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def download(url: str, timeout: int = 30,
             transport: httpx.BaseTransport | None = None) -> bytes:
    """Download a URL as bytes (http/https only)."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"refusing to download non-http(s) URL scheme '{scheme}'")
    with client(timeout, transport=transport) as http:
        response = http.get(url)
        if response.status_code >= 400:
            raise RuntimeError(f"download failed: HTTP {response.status_code}: {response.reason_phrase}")
        return response.content


def extract_text(pdf: str | bytes, max_chars: int | None = None, first_page: bool = False) -> str:
    """Extract text from a PDF using pdftotext.

    pdf: file path (str) or PDF bytes (bytes). Returns plain text; max_chars truncates the head.
    """
    is_bytes = isinstance(pdf, (bytes, bytearray))
    cleanup = None
    if is_bytes:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf)
        path = tmp.name
        cleanup = path
    else:
        path = pdf

    try:
        cmd = ["pdftotext", "-layout"]
        if first_page:
            cmd += ["-f", "1", "-l", "1"]
        cmd += [path, "-"]
        # pdftotext emits UTF-8; decode it explicitly, or a Windows console code page (cp936,
        # cp1252) mangles non-ASCII text.
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        text = proc.stdout if proc.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        text = ""
    finally:
        if cleanup:
            os.unlink(cleanup)

    text = text.strip()
    if max_chars and len(text) > max_chars:
        text = text[: max_chars] + "\n[truncated]"
    return text


def first_lines(text: str, n: int = 6) -> str:
    """Return the first n lines for a preview snippet."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[:n])
