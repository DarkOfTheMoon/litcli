"""从 PDF 首页文本启发式抽取元数据(标题/作者/年份/摘要)→ AcademicItem。

领域库化:from_text_lines 是纯函数(喂任何文本行即可,无 I/O、无 pdftotext 依赖),
可独立复用(纯文本 / OCR 结果 / 他源文本重建元数据);extract_metadata 是薄壳,
读 PDF 首页文本后转调 from_text_lines。完全离线、不调用 LLM。
"""
from __future__ import annotations

import re

from .models import AcademicItem
from . import pdf as pdfutil

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_ABSTRACT_RE = re.compile(r"(^|\b)abstract\b", re.I)
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|&)\s*", re.I)


def _nonempty_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def extract_metadata(pdf_path: str) -> AcademicItem:
    """抽 PDF 首页文本 → AcademicItem。需要系统 pdftotext。"""
    first = pdfutil.extract_text(pdf_path, first_page=True)
    return from_text_lines(_nonempty_lines(first), source=pdf_path)


def from_text_lines(lines: list[str], source: str = "") -> AcademicItem:
    """纯启发式:从文本行构建 AcademicItem(无 I/O、不依赖 pdftotext)。"""
    return AcademicItem(
        source="pdf", key="", title=_guess_title(lines),
        creators=_guess_authors(lines), year=_guess_year(lines),
        abstract=_guess_abstract(lines), item_type="article",
        extra={"source": source},
    )


def _guess_title(lines: list[str]) -> str:
    for ln in lines[:20]:
        if not ln or len(ln) < 12:
            continue
        if _YEAR_RE.search(ln) and len(ln.split()) <= 2:
            continue
        if "@" in ln or ln.startswith("http"):
            continue
        if len(ln.split()) < 3:
            continue
        return _clean_title(ln)
    return _clean_title(lines[0]) if lines else ""


def _clean_title(s: str) -> str:
    s = re.sub(r"^\s*(#|arXiv:\S+\s*)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .:,;")
    return s


def _guess_authors(lines: list[str]) -> list[dict]:
    names: list[str] = []
    started = False
    for ln in lines[:24]:
        if _YEAR_RE.search(ln) or _ABSTRACT_RE.search(ln):
            break
        if not started:
            if _looks_like_author_line(ln):
                started = True
            else:
                continue
        if _looks_like_author_line(ln):
            names.append(ln)
        else:
            break
    return _uniq(_split_authors(names))


def _looks_like_author_line(ln: str) -> bool:
    if not ln or len(ln) < 4 or _YEAR_RE.search(ln):
        return False
    if not re.search(r"\s*[;,]\s*|\band\b|&", ln, re.I):
        return False
    return bool(re.search(r"\b[A-Z][a-z]*\s+[A-Z][a-z]*", ln))


def _split_authors(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for ln in lines:
        for part in _AUTHOR_SPLIT_RE.split(ln):
            part = part.strip(" .,;")
            if not part:
                continue
            tokens = part.split()
            if len(tokens) >= 2:
                out.append({"family": tokens[-1], "given": " ".join(tokens[:-1])})
            elif len(tokens) == 1:
                out.append({"family": tokens[0], "given": ""})
    return out


def _guess_year(lines: list[str]) -> str:
    for ln in lines:
        m = _YEAR_RE.search(ln)
        if m:
            return m.group(0)
    return ""


def _guess_abstract(lines: list[str]) -> str:
    idx = None
    for i, ln in enumerate(lines):
        if _ABSTRACT_RE.search(ln):
            idx = i
            break
    if idx is None:
        return ""
    out = []
    for ln in lines[idx + 1: idx + 12]:
        out.append(ln)
        if len(ln) > 150:
            break
    return " ".join(out)[:1500]


def _uniq(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for a in items:
        k = (a.get("family", ""), a.get("given", ""))
        if k in seen or not k[0]:
            continue
        seen.add(k)
        out.append(a)
    return out
