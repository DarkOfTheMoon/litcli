"""USPTO patent full-text search backend: the United States Patent and Trademark Office open data API.

Read-only, key-gated (free registration; "Get a developer key" at the top of
https://developer.uspto.gov). Public patent records in the key-free open metadata sources
(Crossref/OpenAlex) approach zero, so this backend is the primary channel for patent search — but
like Zotero writes, it only works when an api_key is configured.

Endpoint: ``POST https://api.uspto.gov/api/v1/patent/search``
  Header:  ``X-Api-Key: <key>``
  Body:    ``{"query": {"fq": ["<search term>"]}}``  (fq = full-text fielded query)

Note: field names follow the official USPTO docs; if the real response field names differ, adjust
``_pat_to_item`` — this module is a "key-gated + locally adjustable field" patent entry, not a
dependency on a fixed schema. When no key is configured ``search``/``get`` raises RuntimeError and
the CLI degrades gracefully.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..models import AcademicItem
from .base import Backend

_API = "https://api.uspto.gov/api/v1/patent/search"

# Patent -> CSL/RIS type mapping (base._bibtex_type/to_ris already covers patent)
_TYPE = "patent"


def _url(query: str) -> str:
    """The canonical Google Patents browse page for a patent (a metadata URL, not a download link)."""
    return f"https://patents.google.com/patent/{query}"


def _pat_to_item(rec: dict, source: str = "uspto") -> AcademicItem:
    """Normalize a single USPTO patent record to an AcademicItem (field names per official docs;
    degrade gracefully when fields are missing)."""
    pat = rec or {}
    # USPTO uses patent_patent_id / patent_title / patent_date / patent_abstract, etc.;
    # also compatible with PatentsView-style aliases (patent_id/patent_title/patent_date/patent_abstract).
    pid = pat.get("patent_patent_id") or pat.get("patent_id") or ""
    title = pat.get("patent_title") or pat.get("title") or ""
    date = pat.get("patent_date") or pat.get("patent_date_issued") or ""
    abstract = pat.get("patent_abstract") or pat.get("abstract") or ""
    inventors: list[dict] = []
    inv = pat.get("patent_inventor") or pat.get("inventors") or pat.get("inventor") or []
    for a in inv if isinstance(inv, list) else []:
        if isinstance(a, dict):
            given = a.get("patent_inventor_first_name") or a.get("inventor_name_first") or a.get("first") or ""
            family = a.get("patent_inventor_last_name") or a.get("inventor_name_last") or a.get("last") or ""
        else:
            family, given = str(a), ""
        if family or given:
            inventors.append({"family": family, "given": given})
    year = str(date)[:4] if date else ""
    return AcademicItem(
        source=source,
        key=pid,
        title=title,
        creators=inventors,
        year=year,
        abstract=abstract,
        url=_url(pid) if pid else "",
        item_type=_TYPE,
    )


class UsptoBackend(Backend):
    """USPTO patent search backend (read-only, key-gated)."""

    def __init__(self, name: str = "uspto", api_key: str = "",
                 base: str = _API, max_results: int = 10):
        self.name = name
        self._api_key = api_key
        self._base = base.rstrip("/")
        self._max = max_results

    @property
    def capabilities(self) -> set[str]:
        return {"read"}

    def _require_key(self) -> None:
        if not self._api_key:
            raise RuntimeError(
                f"[{self.name}] api_key not configured: USPTO patent search needs a key from free "
                "registration, please generate one at https://developer.uspto.gov and write it into "
                "the config's api_key")

    def _post(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(body).encode(),
            headers={"User-Agent": f"litcli/0.1 ({self.name})",
                     "Accept": "application/json",
                     "Content-Type": "application/json",
                     "X-Api-Key": self._api_key})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            hint = "check api_key (403/401 usually means an invalid or unauthorized key)" if e.code in (401, 403) else ""
            raise RuntimeError(f"[{self.name}] HTTP {e.code}: {e.reason} {hint}".strip()) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"[{self.name}] unreachable: {e.reason}") from e

    def _results(self, data: dict) -> list[dict]:
        """Extract the results array from the response; tolerate missing fields such as =None."""
        if not isinstance(data, dict):
            return []
        res = data.get("results")
        if isinstance(res, list):
            return res
        if isinstance(data.get("patent"), dict) and isinstance(data["patent"].get("results"), list):
            return data["patent"]["results"]
        return []

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        # Patent full-text search only uses ``fq``; field filters are ignored (the patent
        # identity is application number/title, not bibliographic fields).
        self._require_key()
        body = {"query": {"fq": [query]}, "start": 0, "limit": limit}
        data = self._post(f"{self._base}", body)
        return [_pat_to_item(r, self.name) for r in self._results(data)][:limit]

    def get(self, key: str) -> AcademicItem:
        # Exact patent number query: use fq to exactly match the patent number field
        self._require_key()
        body = {"query": {"fq": [f"patent_id:{key}"]}, "start": 0, "limit": 1}
        data = self._post(f"{self._base}", body)
        rows = self._results(data)
        if not rows:
            raise KeyError(f"[{self.name}] not found: {key}")
        return _pat_to_item(rows[0], self.name)

    def get_attachment(self, key: str) -> bytes:
        # USPTO official full-text PDFs need the key via the print/download endpoint; this does not promise automatic full-text PDF fetching.
        raise RuntimeError(f"[{self.name}] automatic full-text PDF download not yet supported for {key} (USPTO requires separate authorization)")
