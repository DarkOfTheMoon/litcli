"""引文/参考文献渲染(基于 CSL JSON,纯 Python,不依赖 Better BibTeX)。

正统路径:用 ``litcli export --format csljson`` 导出 CSL JSON,喂给
``pandoc --citeproc``/citeproc 生成规范的引文与参考文献表。
这里另提供零依赖的简版 in-text(author-date)渲染,便于 CLI 预览。
"""
from __future__ import annotations

from typing import Iterable

from .models import AcademicItem


def item_to_csl(item: AcademicItem) -> dict:
    return item.to_csl()


def render_intext(items: Iterable[AcademicItem]) -> list[str]:
    """简版 author-date in-text 引用,如 ``Smith, Jones & Lee 2002``。"""
    out = []
    for it in items:
        names = [a.get("family", "") for a in it.creators if isinstance(a, dict)]
        if not names:
            label = "Anon"
        elif len(names) == 1:
            label = names[0]
        elif len(names) == 2:
            label = f"{names[0]} & {names[1]}"
        elif len(names) == 3:
            label = f"{names[0]}, {names[1]} & {names[2]}"
        else:
            label = f"{names[0]} et al."
        out.append(f"({label} {it.year})".strip())
    return out


def render_bibliography(items: Iterable[AcademicItem]) -> list[str]:
    """简版参考文献条目列表,如 ``Smith, J. (2002). Title.``"""
    out = []
    for it in items:
        creators = "; ".join(
            f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
            for a in it.creators if isinstance(a, dict)
        )
        year = f"({it.year})." if it.year else ""
        head = f"{creators} {year}".strip() or "Anon"
        entry = f"{head} {it.title}."
        if it.container_title:
            entry += f" {it.container_title}."
        tail = []
        if it.volume:
            tail.append(f"{it.volume}")
        if it.issue:
            tail.append(f"({it.issue})")
        if it.page:
            tail.append(f"{it.page}")
        if tail:
            entry += " " + ", ".join(tail) + "."
        if it.publisher:
            entry += f" {it.publisher}."
        if it.doi:
            entry += f" doi:{it.doi}"
        if it.url:
            entry += f" {it.url}"
        out.append(entry)
    return out
