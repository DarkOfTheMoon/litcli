"""跨后端聚合领域层:去重/合并/排序,后端无关。

不同后端(如 Zotero 与 arXiv)可能返回同一文献的不同元数据快照。
本模块提供纯函数:归一化、身份键、信息完整度打分与合并,
供 ``--all`` 聚合检索共用;不触碰后端、不依赖网络。
"""
from __future__ import annotations

import copy
import re

from .models import AcademicItem

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def norm_title(title: str) -> str:
    """标题归一:小写、去标点、压缩空白。"""
    t = _PUNCT_RE.sub(" ", (title or "").lower())
    return _WS_RE.sub(" ", t).strip()


def norm_doi(doi: str) -> str:
    """DOI 归一:strip、小写、去尾部句点,并剥掉 https://doi.org/ 前缀。

    不同后端返回的 DOI 形态不同:Crossref 为裸 DOI(10.1103/x),OpenAlex 为
    完整 URL(https://doi.org/10.1103/x)。不剥前缀会使身份键各异,导致 --all
    聚合无法把同一文献的 crossref/openalex 记录合并(见 test_norm_doi_url_prefix)。
    """
    d = (doi or "").strip().lower().rstrip(".")
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d.rstrip(".")


def identity(item: AcademicItem) -> str:
    """身份键:优先生效 DOI,否则退化为归一标题。用于跨库判同。"""
    d = norm_doi(item.doi)
    if d:
        return "doi:" + d
    return "title:" + norm_title(item.title)


def _info_score(item: AcademicItem) -> tuple:
    """信息完整度打分(粗):abstract/doi/url 有则加分,标题长度作尾键。"""
    return (
        1 if item.abstract else 0,
        1 if item.doi else 0,
        1 if item.url else 0,
        len(item.title or ""),
    )


def _merge_two(primary: AcademicItem, other: AcademicItem) -> None:
    """就地把 other 的缺失字段补给 primary(primary 已是副本,可安全改)。"""
    if not primary.creators and other.creators:
        primary.creators = list(other.creators)
    if not primary.year and other.year:
        primary.year = other.year
    if not primary.abstract and other.abstract:
        primary.abstract = other.abstract
    if not primary.url and other.url:
        primary.url = other.url
    if not primary.container_title and other.container_title:
        primary.container_title = other.container_title
    if not primary.volume and other.volume:
        primary.volume = other.volume
    if not primary.issue and other.issue:
        primary.issue = other.issue
    if not primary.page and other.page:
        primary.page = other.page
    if not primary.publisher and other.publisher:
        primary.publisher = other.publisher
    if other.cited_by_count > primary.cited_by_count:
        primary.cited_by_count = other.cited_by_count
    oaid = (other.extra or {}).get("openalex_id")
    if oaid and not (primary.extra or {}).get("openalex_id"):
        primary.extra["openalex_id"] = oaid
    for a in other.attachments:
        if a not in primary.attachments:
            primary.attachments.append(a)


def _year_key(year: str) -> int:
    """年份降序排序键(非数字按 0 处理)。"""
    return -int(year) if year.isdigit() else 0


def _cited_key(item: AcademicItem) -> int:
    """Citation-count descending sort key (treats no-citation records as 0)."""
    return -item.cited_by_count


def merge_items(items: list[AcademicItem]) -> list[AcademicItem]:
    """去重 + 合并 + 排序。

    - 按 :func:`identity` 分组,保留首次出现顺序;
    - 每组取信息最完整者为主记录,其余补给缺失字段(副本操作,不污染入参);
    - ``extra["sources"]`` 记录全部来源,``source`` 保留主记录来源;
    - 最终按年份降序,同年按标题升序稳定排序。
    """
    groups: dict[str, list[AcademicItem]] = {}
    order: list[str] = []
    for it in items:
        k = identity(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)

    merged: list[AcademicItem] = []
    for k in order:
        group = groups[k]
        primary = copy.deepcopy(max(group, key=_info_score))
        for other in group:
            if other is primary:
                continue
            _merge_two(primary, other)
        srcs = sorted({it.source for it in group if it.source})
        if srcs:
            primary.extra["sources"] = srcs
        merged.append(primary)

    merged.sort(key=lambda it: (_year_key(it.year), norm_title(it.title)))
    return merged
