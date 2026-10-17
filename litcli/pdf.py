"""PDF handling: download + text extraction (via the system pdftotext CLI), no Python deps.

Backend-agnostic: Zotero attachments and arXiv direct links are both normalized to
"given bytes/path -> extract text".
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.request


def download(url: str, timeout: int = 30) -> bytes:
    """Download a URL as bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "litcli/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def extract_text(pdf: str | bytes, max_chars: int | None = None, first_page: bool = False) -> str:
    """Extract text from a PDF using pdftotext.

    pdf: file path (str) or PDF bytes (bytes). Returns plain text; max_chars truncates the head.
    """
    is_bytes = isinstance(pdf, (bytes, bytearray))
    cleanup = None
    if is_bytes:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(pdf)
        tmp.close()
        path = tmp.name
        cleanup = path
    else:
        path = pdf

    try:
        cmd = ["pdftotext", "-layout"]
        if first_page:
            cmd += ["-f", "1", "-l", "1"]
        cmd += [path, "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
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
