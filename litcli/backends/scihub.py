"""Sci-Hub fulltext fallback backend (DOI -> PDF), read-only.

Sci-Hub is not a bibliographic search engine: in 2026 its home form accepts only a DOI / article
URL (the old ``/search?query=`` endpoint returns an empty body), so this backend resolves *by
DOI*. ``get(doi)`` returns a minimal item carrying the PDF link in ``attachments``;
``get_attachment(doi)`` downloads the bytes. It sits at the **end** of the ``fetch fulltext``
chain, after the legitimate open-access sources (local store -> Unpaywall -> OpenAlex), and is
opt-in — anything Sci-Hub returns is copyright-evading content, not an OA licence.

Reaching an article takes two gates, both passable locally:

* a DDoS-Guard cookie wall, handled by a per-call cookie jar (the same session a browser
  would carry), and
* an ALTCHA proof-of-work captcha — ``/captcha/challenge/<id>`` returns a SHA-256 target; the
  page's own JavaScript brute-forces a ``number`` whose hash matches, then POSTs the base64
  payload to ``/captcha/solution/<id>``. :func:`solve_altcha` does the same work locally.

Nothing is bypassed in the "break the anti-bot" sense: the PoW is solved exactly as the page
asks. Only public identifiers and the PDF itself are fetched, one DOI per call.

Markup is split into pure functions (:func:`extract_pdf_url`, :func:`parse_citation_meta`,
:func:`solve_altcha`, :func:`captcha_id`) so a site change touches only the thin adapter.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from urllib.parse import urljoin

import httpx

from .. import pdf as pdfutil
from ..models import AcademicItem, extract_year, normalize_author_name
from ..transport import Session
from .base import Backend

# Mirrors tried in order; overridable via ``[backends.scihub].mirrors``. Probed 2026-10: ``.ru``
# serves the captcha/article page, ``.st`` answers 403 (edge wall), ``.se`` fails DNS here.
_DEFAULT_MIRRORS = ("sci-hub.ru", "sci-hub.st", "sci-hub.se")

# A browser-ish UA; the mirrors' edge rejects obviously scripted clients.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0 Safari/537.36 (litcli)")

# Captcha page marker: ``/captcha/challenge/<id>`` (also present as ``/captcha/solution/<id>``).
_CAPTCHA_ID_RE = re.compile(r"/captcha/(?:challenge|solution)/(\d+)")

# PDF link fallbacks: the ``#pdf`` container's ``<embed>`` / ``<iframe>`` src.
_EMBED_RE = re.compile(r"<(?:embed|iframe)\b[^>]*\bsrc=[\"']([^\"']+)", re.I)

# ``<meta ...>`` tags (attribute order varies, so the tag is scanned rather than pattern-matched).
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_NAME_RE = re.compile(r"\bname=[\"']([^\"']+)[\"']", re.I)
_META_CONTENT_RE = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)

# ALTCHA hash algorithms the widget supports (``hashlib`` names).
_ALGORITHMS = {"SHA-256": "sha256", "SHA-384": "sha384", "SHA-512": "sha512"}

# Safety cap so a pathological ``maxNumber`` cannot hang the CLI.
_MAX_TRIES = 5_000_000

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


# --- pure helpers (no network) ---

def doi_from(key: str) -> str:
    """Extract a bare DOI from a key form (``scihub/<doi>``, doi.org URL, ``doi:``, bare DOI).

    Returns "" when the key is not DOI-shaped (e.g. a free-text title), which is how
    :meth:`ScihubBackend.search` tells a resolvable query from a keyword one.
    """
    k = (key or "").strip()
    if k.startswith("scihub/"):
        k = k[len("scihub/"):]
    k = _DOI_PREFIX_RE.sub("", k).strip()
    return k if _DOI_RE.match(k) else ""


def captcha_id(html: str) -> str:
    """Return the captcha session id from a challenge page, or "" when the page is not one."""
    m = _CAPTCHA_ID_RE.search(html or "")
    return m.group(1) if m else ""


def solve_altcha(challenge: dict) -> str:
    """Solve an ALTCHA proof-of-work challenge and return the base64 payload to POST back.

    The payload shape mirrors the widget's own: ``base64(json({algorithm, challenge, number,
    salt, signature}))``. The salt is hashed verbatim, including its ``?expires=...&`` suffix —
    that suffix is part of the signed string, not a URL query.
    """
    challenge = challenge or {}
    algorithm = str(challenge.get("algorithm", ""))
    salt = challenge.get("salt") or ""
    target = challenge.get("challenge") or ""
    hashlib_name = _ALGORITHMS.get(algorithm.upper())
    if not hashlib_name or not salt or not target:
        raise RuntimeError("malformed captcha challenge")
    max_number = min(int(challenge.get("maxNumber") or _MAX_TRIES), _MAX_TRIES)
    for number in range(max_number + 1):
        if hashlib.new(hashlib_name, (salt + str(number)).encode()).hexdigest() == target:
            payload = {"algorithm": algorithm, "challenge": target, "number": number,
                       "salt": salt, "signature": challenge.get("signature", "")}
            return base64.b64encode(
                json.dumps(payload, separators=(",", ":")).encode()).decode()
    raise RuntimeError(f"captcha unsolved within {max_number} tries")


def _metas(html: str) -> dict[str, list[str]]:
    """Collect ``<meta name="citation_*" content=...>`` values, keyed by lowercased name."""
    found: dict[str, list[str]] = {}
    for tag in _META_TAG_RE.findall(html or ""):
        name = _META_NAME_RE.search(tag)
        content = _META_CONTENT_RE.search(tag)
        if not name or not content:
            continue
        n = name.group(1).lower()
        if n.startswith("citation_"):
            found.setdefault(n, []).append(content.group(1))
    return found


def extract_pdf_url(html: str, page_url: str = "") -> str:
    """Return the absolute PDF link from an article page, or "" (meta first, embed/iframe next)."""
    metas = _metas(html)
    raw = (metas.get("citation_pdf_url") or [""])[0]
    if not raw:
        m = _EMBED_RE.search(html or "")
        raw = m.group(1) if m else ""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    return urljoin(page_url, raw)


def parse_citation_meta(html: str) -> dict:
    """Extract the article's own ``citation_*`` metadata (title / authors / journal / date / doi)."""
    metas = _metas(html)
    return {
        "title": ((metas.get("citation_title") or [""])[0]).strip(),
        "authors": [a.strip() for a in metas.get("citation_author") or [] if a.strip()],
        "container": ((metas.get("citation_journal_title")
                       or metas.get("citation_conference_title") or [""])[0]).strip(),
        "year": ((metas.get("citation_publication_date")
                  or metas.get("citation_date") or metas.get("citation_year") or [""])[0]).strip(),
        "doi": ((metas.get("citation_doi") or [""])[0]).strip(),
    }


def _mirror_base(mirror: str) -> str:
    """Normalize a mirror to an ``https://host`` base (a scheme may be given explicitly)."""
    m = (mirror or "").strip().rstrip("/")
    if not m:
        raise RuntimeError("empty mirror")
    return m if m.startswith(("http://", "https://")) else f"https://{m}"


# --- HTTP adapter (injectable for offline tests) ---

class _Http(Session):
    """Cookie-keeping session for one bot-check run (also the base for zlibrary)."""

    def __init__(self, timeout: float = 30, transport=None):
        super().__init__(timeout, ua=_UA, transport=transport,
                         headers={"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})

    def _request(self, url: str, data: bytes | None = None,
                 headers: dict | None = None) -> tuple[str, str]:
        kw: dict = {}
        if headers:
            kw["headers"] = headers
        if data is not None:
            kw["content"] = data
        try:
            r = self.client.request("GET" if data is None else "POST", url, **kw)
        except httpx.HTTPError as e:
            raise RuntimeError(f"unreachable: {e}") from e
        if r.status_code == 404:  # "article not found" is a page state, not a transport failure
            return str(r.url), ""
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.reason_phrase}")
        return str(r.url), r.text

    def get_text(self, url: str) -> tuple[str, str]:
        return self._request(url)

    def get_json(self, url: str) -> dict:
        _, body = self._request(url, headers={"Accept": "application/json"})
        try:
            return json.loads(body)
        except ValueError as e:
            raise RuntimeError("captcha challenge was not JSON") from e

    def post_json(self, url: str, payload: dict) -> dict:
        _, body = self._request(
            url, data=json.dumps(payload).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json"})
        try:
            return json.loads(body)
        except ValueError as e:
            raise RuntimeError("captcha response was not JSON") from e


def _pass_captcha(http: _Http, base: str, page_url: str, html: str) -> tuple[str, str]:
    """Solve the page's PoW, POST the solution, reload, and return the unlocked page."""
    cid = captcha_id(html)
    if not cid:
        raise RuntimeError("captcha page has no challenge id")
    payload = solve_altcha(http.get_json(f"{base}/captcha/challenge/{cid}"))
    result = http.post_json(f"{base}/captcha/solution/{cid}", {"captcha": payload})
    if not result.get("success"):
        raise RuntimeError("captcha solution rejected")
    page_url, html = http.get_text(page_url)
    if captcha_id(html):
        raise RuntimeError("captcha still required after solving")
    return page_url, html


def resolve(doi: str, mirrors=None, timeout: int = 30, http: _Http | None = None) -> dict:
    """Resolve a DOI to a PDF link, trying each mirror in order.

    Returns ``{"doi", "mirror", "page_url", "pdf_url", "meta"}``. Raises ``KeyError`` when every
    reachable mirror says the DOI is not in the library, ``RuntimeError`` when all mirrors fail
    for transport / captcha reasons.
    """
    clean = doi_from(doi)
    if not clean:
        raise KeyError(f"[scihub] not a DOI: {doi!r}")
    own = http is None
    http = http or _Http(timeout)
    not_found = 0
    errors: list[str] = []
    try:
        for mirror in (mirrors or _DEFAULT_MIRRORS):
            try:
                base = _mirror_base(mirror)
                page_url, html = http.get_text(f"{base}/{urllib.parse.quote(clean, safe='/')}")
                if not html:
                    not_found += 1
                    continue
                if captcha_id(html):
                    page_url, html = _pass_captcha(http, base, page_url, html)
                pdf_url = extract_pdf_url(html, page_url)
                if not pdf_url:
                    not_found += 1
                    continue
                return {"doi": clean, "mirror": mirror, "page_url": page_url,
                        "pdf_url": pdf_url, "meta": parse_citation_meta(html)}
            except RuntimeError as e:
                errors.append(f"{mirror}: {e}")
        if not_found:
            raise KeyError(f"[scihub] {clean} is not in the library")
        raise RuntimeError(f"[scihub] all mirrors failed ({'; '.join(errors)})")
    finally:
        if own:
            http.close()


class ScihubBackend(Backend):
    """Sci-Hub DOI -> PDF fallback (read-only; opt-in, last in the fulltext chain)."""

    name = "scihub"

    @property
    def capabilities(self) -> set[str]:
        # Read-only. It deliberately does NOT advertise "attachment": the fulltext chain must
        # treat a Sci-Hub hit as a *fallback*, not as a local-store attachment (which wins).
        return {"read"}

    def __init__(self, name: str = "scihub", mirrors=None, timeout: int = 30):
        self.name = name
        self._mirrors = list(mirrors or _DEFAULT_MIRRORS)
        self._timeout = int(timeout)

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        """Resolve a DOI-shaped query; keyword queries return [] (Sci-Hub has no search index)."""
        if not doi_from(query):
            return []
        try:
            return [self.get(query)][:limit]
        except KeyError:  # not in the library: an empty result, not an error, for search
            return []

    def get(self, key: str) -> AcademicItem:
        """Resolve one DOI and return its metadata plus the PDF link in ``attachments``."""
        if not key:
            raise KeyError(f"[{self.name}] empty key")
        result = resolve(key, self._mirrors, self._timeout)
        meta = result["meta"]
        doi = meta["doi"] or result["doi"]
        # Some records append a literal "et., al." author; it is not a person.
        creators = [normalize_author_name(a) for a in meta["authors"]
                    if re.sub(r"[\s.,]", "", a).lower() not in ("", "etal")]
        return AcademicItem(
            source=self.name, key=result["doi"], title=meta["title"], doi=doi,
            creators=[c for c in creators if c.get("family") or c.get("given")],
            year=extract_year(meta["year"]), container_title=meta["container"],
            url=f"https://doi.org/{doi}", item_type="article",
            attachments=[result["pdf_url"]],
            extra={"scihub": True, "mirror": result["mirror"],
                   "scihub_page": result["page_url"]},
        )

    def get_attachment(self, key: str) -> bytes:
        """Download the resolved PDF bytes."""
        return pdfutil.download(self.get(key).attachments[0], timeout=self._timeout)
