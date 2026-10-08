"""Unpaywall 解析:经 DOI 反查合法开放获取(OA)全文链接。

只读、按 DOI(不做搜索)。Unpaywall 要求有效的 email 参数。这是 SCI-Hub 等
版权规避源之外的**合规全文**通道——能拿到开放获取版本就给出可下载直链。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

_API = "https://api.unpaywall.org/v2"

# 匿名默认邮箱(Unpaywall 仅用作配额标识;正式使用建议从 config 或环境提供)
DEFAULT_EMAIL = "litcli@anonymous.com"


def _best_oa(data: dict) -> dict:
    loc = data.get("best_oa_location") or {}
    return {
        "is_oa": bool(data.get("is_oa", False)),
        "oa_status": data.get("oa_status", ""),
        "best_pdf": loc.get("url_for_pdf") or "",
        "best_landing": loc.get("url_for_landing_page") or "",
    }


def resolve_oa(doi: str, email: str = DEFAULT_EMAIL, timeout: int = 25) -> dict:
    """按 DOI 查 OA 信息:返回 is_oa / oa_status / best_pdf / best_landing / title / year。"""
    url = f"{_API}/{urllib.parse.quote(doi, safe='')}?email={urllib.parse.quote(email)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "litcli/0.1 (oa resolver)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        hint = "email 参数可能是 422 原因(请提供有效 email)" if e.code == 422 else ""
        raise RuntimeError(f"[unpaywall] HTTP {e.code}: {e.reason} {hint}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"[unpaywall] 无法访问: {e.reason}") from e

    info = _best_oa(data)
    info["title"] = data.get("title", "")
    info["year"] = data.get("year", "")
    info["doi"] = doi
    return info
