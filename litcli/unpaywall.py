"""Unpaywall resolution: reverse-lookup a legitimate open-access (OA) fulltext link by DOI.

Read-only, by DOI (no search). Unpaywall requires a valid email parameter. This is the
**legitimate fulltext** channel outside copyright-evading sources like Sci-Hub — when an
open-access version is available, it gives a downloadable direct link.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

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


def resolve_oa(doi: str, email: str = DEFAULT_EMAIL, timeout: int = 25) -> dict:
    """Look up OA info by DOI: returns is_oa / oa_status / best_pdf / best_landing / title / year."""
    url = f"{_API}/{urllib.parse.quote(doi, safe='')}?email={urllib.parse.quote(email)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "litcli/0.1 (oa resolver)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        hint = "the email param may be the 422 cause (provide a valid email)" if e.code == 422 else ""
        raise RuntimeError(f"[unpaywall] HTTP {e.code}: {e.reason} {hint}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"[unpaywall] cannot access: {e.reason}") from e

    info = _best_oa(data)
    info["title"] = data.get("title", "")
    info["year"] = data.get("year", "")
    info["doi"] = doi
    return info
