"""合法全文解析链:本地库 → 公开 OA 源 → 指路图书馆/代理。

三条优先级(按用户要求):
  1. **本地库**:遍历配置后端里带 attachment 能力的(如 Zotero),按 DOI 定位条目并取附件——本地库有就优先,不走网络。
  2. **公开 OA 源**:Unpaywall(best_oa_location.pdf) → OpenAlex(best_oa_location.pdf_url),发布者自提供的合法开放获取版本。
  3. **指路**:以上都拿不到,返回 has_fulltext=False,提示走机构订阅/校际互借/代理——**绝不绕过付费墙**。

这是 Sci-Hub 类版权规避源的合规替代。与后端解耦:``resolve_fulltext`` 接受可注入的解析器列表便于离线桩测,默认解析器为 local/unpaywall/openalex。
"""
from __future__ import annotations

import urllib.error

from . import unpaywall
from .models import AcademicItem

# 一个解析器:输入 doi,返回候选 dict 或 None。
# 候选可用于 CLI 的字段:
#   has_fulltext, kind('local'|'unpaywall'|'openalex'), url(OA 时),
#   data(本地附件 bytes 时), title, status, key
Resolver = callable


def _norm_doi(d: str) -> str:
    return (d or "").strip().lower()


def _from_local(doi: str, backends: dict | None = None) -> dict | None:
    """本地库优先:对带 attachment 的后端,按 DOI 定位条目并取附件。"""
    if not backends:
        return None
    want = _norm_doi(doi)
    for name, b in backends.items():
        if not b.can("attachment"):
            continue
        try:
            for item in b.search(doi, limit=8):
                if _norm_doi(item.doi) != want and _norm_doi(item.url) != want:
                    continue
                try:
                    data = b.get_attachment(item.key)
                except (RuntimeError, NotImplementedError, OSError):
                    continue
                if data:
                    return {"kind": "local", "data": data, "title": item.title,
                            "key": item.key, "source": name}
        except (RuntimeError, urllib.error.URLError, OSError, NotImplementedError):
            continue
    return None


def _from_unpaywall(doi: str, email: str) -> dict | None:
    """Unpaywall 反查合法 OA PDF(best_oa_location.url_for_pdf)。"""
    try:
        info = unpaywall.resolve_oa(doi, email)
    except (RuntimeError, urllib.error.URLError, OSError):
        return None
    url = info.get("best_pdf") or info.get("best_landing") or ""
    if not url:
        return None
    return {"kind": "unpaywall", "url": url, "title": info.get("title", ""),
            "status": info.get("oa_status", "")}


def _from_openalex(doi: str, email: str) -> dict | None:
    """OpenAlex best_oa_location.pdf_url(聚合的 OA 直链)。"""
    try:
        from .backends import openalex
    except ImportError:
        return None
    try:
        b = openalex.OpenAlexBackend(mailto=email)
        item: AcademicItem = b.get(doi)
    except (RuntimeError, urllib.error.URLError, OSError, KeyError):
        return None
    if item.attachments:
        return {"kind": "openalex", "url": item.attachments[0],
                "title": item.title, "status": ""}
    return None


def _default_resolvers(email: str, backends: dict | None = None) -> list[Resolver]:
    return [
        lambda d: _from_local(d, backends),
        lambda d: _from_unpaywall(d, email),
        lambda d: _from_openalex(d, email),
    ]


def resolve_fulltext(doi: str, email: str = unpaywall.DEFAULT_EMAIL,
                     backends: dict | None = None,
                     resolvers: list[Resolver] | None = None) -> dict:
    """按 DOI 找合法全文,优先级:本地库 → OA 源。

    返回 dict:有候选时 ``{"has_fulltext": True, "kind", ...}``(本地为 data,OA 为 url);
    无任何合法全文时 ``{"has_fulltext": False}``(附各步失败原因,供 CLI 指路)。
    """
    if resolvers is None:
        resolvers = _default_resolvers(email, backends)
    errors: list[str] = []
    for r in resolvers:
        try:
            cand = r(doi)
        except Exception as e:  # 单个解析器失败不拖垮整体
            errors.append(f"{getattr(r, '__name__', type(r).__name__)}: {e}")
            continue
        if cand and (cand.get("url") or cand.get("data")):
            cand["has_fulltext"] = True
            return cand
    return {"has_fulltext": False, "errors": errors}


# --- 收录准入(防本地库爆库) ---


def should_import(candidate: AcademicItem, existing: list[AcademicItem]) -> tuple[bool, str]:
    """判定一条候选文献是否值得收录进本地库(硬性查重门)。

    返回 (允许, 原因)。规则:
      - 候选有 DOI:与任一已有条目的 DOI 归一相等 → 拒绝(重复)。
      - 候选无 DOI:用归一标题判同 → 标题一致 → 拒绝。
      - 元数据不完整(无标题/无作者/无年份) → 默认拒绝(半残条目不自动收),
        除非调用方显式 --force(由 CLI 决定)。
    复用 :mod:`aggregate` 的 identity / norm_doi / norm_title 判同,防库爆炸。
    """
    from . import aggregate

    cand_doi = aggregate.norm_doi(candidate.doi)
    cand_title = aggregate.norm_title(candidate.title)
    for ex in existing:
        ex_doi = aggregate.norm_doi(ex.doi)
        if cand_doi and ex_doi and cand_doi == ex_doi:
            return False, f"库内已存在同 DOI 条目({ex.key or ex.title[:30]})"
        if not cand_doi:
            ex_title = aggregate.norm_title(ex.title)
            if ex_title and cand_title and ex_title == cand_title:
                return False, f"库内已存在同题条目({ex.key or ex.title[:30]})"
    if not candidate.title:
        return False, "缺标题,半残条目;请补全或 --force"
    if not candidate.creators and not candidate.year:
        return False, "无作者且无年份,半残条目;请补全或 --force"
    return True, ""
