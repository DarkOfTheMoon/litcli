"""PDF 处理:下载 + 文本抽取(经系统 pdftotext CLI),零 Python 依赖。

后端无关——Zotero 附件与 arXiv 直链都归一化为「拿到 bytes/路径 → 抽文本」。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.request


def download(url: str, timeout: int = 30) -> bytes:
    """下载 URL 为 bytes。"""
    req = urllib.request.Request(url, headers={"User-Agent": "litcli/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def extract_text(pdf: str | bytes, max_chars: int | None = None, first_page: bool = False) -> str:
    """用 pdftotext 抽取 PDF 文本。

    pdf: 文件路径(str)或 PDF 字节(bytes)。返回纯文本;max_chars 截断首部。
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
        text = text[: max_chars] + "\n[…截断]"
    return text


def first_lines(text: str, n: int = 6) -> str:
    """取前 n 行用于预览摘要。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[:n])
