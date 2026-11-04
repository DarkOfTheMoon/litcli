"""Unpaywall resolution: reverse-lookup a legitimate open-access (OA) fulltext link by DOI.

Read-only, by DOI (no search). Unpaywall requires a valid email parameter. This is the
**legitimate fulltext** channel outside copyright-evading sources like Sci-Hub — when an
open-access version is available, it gives a downloadable direct link.
"""
from __future__ import annotations

import urllib.parse

import httpx

from .transport import client

_API = "https://api.unpaywall.org/v2"

# Anonymous default email (Unpaywall uses it only as a quota identifier; for real use, provide it via config or env)
DEFAULT_EMAIL = "litcli@anonymous.com"


def _best_oa(data: dict) -> dict:
    loc = data.get("best_oa_location") or {}
    return {
        "is_oa": bool(data.get("is_oa", False)),
        "oa_status": data.get("oa_status", ""),
        "best_pdf": loc.get("url_for_pdf") or "",
        "best_landing": loc.get("url_for_landing_page") or "",
    }


def resolve_oa(doi: str, email: str = DEFAULT_EMAIL, timeout: int = 25,
               transport: httpx.BaseTransport | None = None) -> dict:
    """Look up OA info by DOI: returns is_oa / oa_status / best_pdf / best_landing / title / year."""
    url = f"{_API}/{urllib.parse.quote(doi, safe='')}?email={urllib.parse.quote(email)}"
    try:
        with client(timeout, ua="litcli/0.1 (oa resolver)", transport=transport) as http:
            response = http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"[unpaywall] cannot access: {e}") from e
    if response.status_code >= 400:
        hint = (" the email param may be the 422 cause (provide a valid email)"
                if response.status_code == 422 else "")
        raise RuntimeError(f"[unpaywall] HTTP {response.status_code}: "
                           f"{response.reason_phrase}{hint}")
    data = response.json()

    info = _best_oa(data)
    info["title"] = data.get("title", "")
    info["year"] = data.get("year", "")
    info["doi"] = doi
    return info
