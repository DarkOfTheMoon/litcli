"""Zotero local library backend (Zotero 10, localhost:23119 local API).

Capabilities: read + write (get_attachment). The local API needs no key; it always sends
Zotero-API-Version: 3. The local user library uses the ``users/0`` alias (the response expands
to the real library id). Writes use the If-Unmodified-Since-Version concurrency-control header.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from ..models import AcademicItem
from .base import Backend

_ITEMTYPE_MAP = {
    "journalArticle": "article",
    "book": "book",
    "bookSection": "chapter",
    "conferencePaper": "paper-conference",
    "report": "report",
    "document": "document",
    "thesis": "thesis",
    "preprint": "article",
    "reportArticle": "article",
}

# Zotero write-JSON field names (camelCase) and the AcademicItem correspondence
_ZOTERO_RESPONSE_FIELDS = ("version", "key")


def first_digit_year(text: str) -> str:
    """Extract a 4-digit year from Zotero's ``date`` field, e.g. '2002' / '2020-03-12' -> '2002'."""
    if not text:
        return ""
    for tok in text.replace("/", "-").split("-"):
        tok = tok.strip()
        if tok.isdigit() and len(tok) >= 4:
            return tok[:4]
    for tok in text.replace("/", " ").split():
        if tok.isdigit() and len(tok) == 4:
            return tok
    return text[:4] if text[:4].isdigit() else ""


def _parse_creator(c: dict) -> dict | None:
    if not isinstance(c, dict):
        return None
    fam = c.get("lastName") or c.get("name") or c.get("family", "")
    giv = c.get("firstName") or c.get("given", "")
    if not (fam or giv):
        return None
    return {"family": fam, "given": giv}


def _item_to_academic(data: dict, source: str = "zotero") -> AcademicItem:
    """Zotero item.data JSON -> AcademicItem."""
    creators = [p for p in (_parse_creator(c) for c in data.get("creators", [])) if p]
    item_type = _ITEMTYPE_MAP.get(data.get("itemType", ""), "document")
    raw_date = data.get("date", "")
    # Container name prefers publicationTitle (journal), falling back to bookTitle/seriesTitle/encyclopediaTitle
    container = data.get("publicationTitle") or data.get("bookTitle") or data.get("seriesTitle")
    return AcademicItem(
        source=source,
        key=data.get("key", ""),
        title=data.get("title", ""),
        creators=creators,
        year=first_digit_year(raw_date),
        abstract=data.get("abstractNote", ""),
        doi=data.get("DOI", ""),
        url=data.get("url", ""),
        container_title=container or "",
        volume=data.get("volume", "") or "",
        issue=data.get("issue", "") or "",
        page=data.get("pages", "") or "",
        publisher=data.get("publisher", "") or "",
        item_type=item_type,
        extra={"date": raw_date, "zoteroItemType": data.get("itemType", "")},
    )


class ZoteroLocalBackend(Backend):
    def __init__(self, name: str = "zotero", base: str = "http://localhost:23119/api",
                 library: str = "0", api_key: str | None = None, app_name: str = "litcli"):
        self.name = name
        self._base = base.rstrip("/")
        self._users = f"users/{library or '0'}"
        self._api_key = api_key
        self._app_name = app_name
        self._app_key_val: str | None = None
        self._server_id_val: str | None = None

    @property
    def capabilities(self) -> set[str]:
        return {"read", "write", "attachment"}

    # --- Low-level requests ---

    def _url(self, path: str) -> str:
        return f"{self._base}/{path.lstrip('/')}"

    def _headers(self) -> dict:
        # Reads can impersonate localhost; writes obtain a local key via /api/local/authorize (see _write_key).
        # Reads use "localhost"; writes use a locally authorized key (auto-obtained or the config-injected api_key).
        key = self._api_key or self._app_key_val or "localhost"
        return {
            "Zotero-API-Version": "3",
            "Zotero-API-Key": key,
            "Content-Type": "application/json",
        }

    def _do_authorize(self) -> str:
        """The real /api/local/authorize handshake: asks Zotero to pop the local authorization dialog
        and returns the local key."""
        body = json.dumps({"appName": self._app_name}).encode()
        req = urllib.request.Request(
            self._url("local/authorize"), data=body, method="POST",
            headers={"Zotero-API-Version": "3",
                     "Zotero-Server-ID": self._server_id(),
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:  # Wait for the user's UI approval
            data = json.loads(r.read())
            return data.get("key", "")

    def authorize(self) -> str:
        """Force a local authorization handshake and return the key, caching it (for the CLI
        `backends authorize`).

        Each call triggers the Zotero local authorization dialog (unless the app already remembers
        an authorization); the returned key is a locally valid key, not a Web API key generated by
        zotero.org. Raises RuntimeError on failure.
        """
        try:
            key = self._do_authorize()
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"[{self.name}] local authorization failed: "
                               f"{getattr(e, 'reason', e)}") from e
        self._app_key_val = key
        return key

    def _write_key(self) -> str:
        """The local authorization key needed for write operations.

        Priority: the config-injected api_key > the _app_key_val obtained at runtime via authorize.
        When no key is set, calls /api/local/authorize to obtain a local API key (triggers the Zotero
        UI authorization dialog).
        """
        if self._api_key:
            return self._api_key
        if self._app_key_val:
            return self._app_key_val
        self._app_key_val = self.authorize()
        return self._app_key_val

    def _server_id(self) -> str:
        """Zotero 10 local write requests need the Zotero-Server-ID header (to guard against
        cross-instance use).

        This value comes from the **response header of /api requests** (not the /connector/ping body,
        which is HTML "Zotero is running"); every /api response carries it. Lazily issues a tiny GET and
        caches the Server-ID from its response header into _server_id_val (also cached in _request).

        Note: the cache field name _server_id_val is kept separate from the instance method _server_id —
        if they shared a name (e.g. self._server_id = None), the method body would resolve self._server_id
        as a bound method, so the read would never execute and would return a method object instead of a
        string.
        """
        if not self._server_id_val:
            try:
                self._json_path(f"/{self._users}/items", {"limit": 1, "format": "json"})
            except Exception:
                self._server_id_val = ""
        return self._server_id_val

    def _cache_server_id(self, hdr: dict) -> None:
        """Cache the Zotero-Server-ID from the /api response header (needed to echo it back on write
        requests)."""
        sid = hdr.get("Zotero-Server-ID") or hdr.get("zotero-server-id")
        if sid:
            self._server_id_val = sid

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, extra_headers: dict | None = None):
        url = self._url(path)
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"
        headers = dict(self._headers())
        if extra_headers:
            headers.update(extra_headers)
        # Write operations (POST/PATCH/DELETE, or with a body) need Zotero-Server-ID + the local authorization key
        if method != "GET" or body is not None:
            sid = self._server_id()
            if sid:
                headers.setdefault("Zotero-Server-ID", sid)
            if not self._api_key:
                # Lazily obtain the local authorization key (first time only; skipped when config provides an api_key)
                try:
                    self._write_key()
                except RuntimeError:
                    pass
                headers["Zotero-API-Key"] = self._app_key_val or "localhost"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                hdr = dict(resp.headers)
                self._cache_server_id(hdr)
                return resp.status, hdr, raw
        except urllib.error.HTTPError as e:
            hdr = dict(e.headers)
            self._cache_server_id(hdr)
            return e.code, hdr, e.read()

    def _json_path(self, path: str, params: dict | None = None):
        status, headers, raw = self._request("GET", path, params)
        if status >= 400:
            raise RuntimeError(f"[{self.name}] GET {path} -> HTTP {status}: {raw[:400]!r}")
        return headers, (json.loads(raw) if raw else None)

    # --- Backend interface ---

    def search(self, query: str, limit: int = 10, filters: dict | None = None) -> list[AcademicItem]:
        # qmode=everything makes ``q`` cover all fields (incl. abstract), not the default
        # titleCreatorYear; Chinese-only or abstract-only terms (e.g. a term found only in the abstract) need it to hit.
        # Zotero has a single ``q``, so field words fold into it (everything-mode full-text hit).
        filt = self._filters(filters)
        terms = [t for t in (query, filt.get("title"), filt.get("author"), filt.get("container")) if t]
        if filt.get("year") and filt["year"].isdigit():
            terms.append(filt["year"])
        q = " ".join(terms) or query
        headers, data = self._json_path(
            f"/{self._users}/items",
            {"q": q, "qmode": "everything", "limit": limit, "format": "json"})
        return [_item_to_academic(it["data"], self.name) for it in (data or []) if "data" in it]

    def get(self, key: str) -> AcademicItem:
        _, data = self._json_path(f"/{self._users}/items/{key}")
        item = _item_to_academic(data["data"], self.name)
        item.extra["version"] = data.get("version", "")
        return item

    def list_collections(self) -> list[dict]:
        _, data = self._json_path(f"/{self._users}/collections", {"limit": 100})
        return [
            {"key": c["key"], "name": c["data"].get("name", "")}
            for c in (data or []) if "data" in c
        ]

    def ping(self) -> bool:
        """Lightweight reachability probe: a single-item/single-page GET; success (<400) means the local
        library is running. For `backends status`."""
        try:
            status, _, _ = self._request("GET", f"/{self._users}/items", {"limit": 1, "format": "json"})
            return status is not None and status < 400
        except Exception:
            return False

    def _paginate(self, path: str, params: dict, size: int = 100) -> list[dict]:
        """Paginate over a path's full raw item dicts, page size (default 100), with light sleeps between
        batches + per-batch retries.

        Avoids Zotero's occasional timeouts on oversized single-page responses; returns the raw item
        dict list across all pages.
        """
        rows_all: list[dict] = []
        start = 0
        while True:
            page_params = dict(params, start=start)
            rows = None
            for attempt in range(3):
                try:
                    _, data = self._json_path(path, dict(page_params, limit=size))
                    rows = data or []
                    break
                except (urllib.error.URLError, OSError, RuntimeError) as e:
                    if attempt == 2:
                        raise RuntimeError(f"[{self.name}] failed to fetch {path} start={start}: {e}") from e
                    time.sleep(0.3 * (attempt + 1))
            rows_all.extend(rows)
            if len(rows) < size:
                break
            start += size
            time.sleep(0.2)
        return rows_all

    def collection_tree(self) -> list[dict]:
        """Fetch all collection nodes (including parent), for the CLI to render the collection tree.
        Paginates the full set; parent is the parent."""
        nodes = []
        raw = self._paginate(f"/{self._users}/collections", {"format": "json"})
        for c in raw:
            d = c.get("data", {})
            if d:
                nodes.append({
                    "key": d.get("key", ""),
                    "name": d.get("name", ""),
                    "parent": d.get("parentCollection", ""),
                })
        return nodes

    def collection_items(self, coll_key: str, recursive: bool = True) -> list[AcademicItem]:
        """List items in a collection. Note: Zotero /items?collection=X is actually ignored (returns the
        whole library), so you must pull the whole library's items and then attribute each locally via
        its ``collections`` field.

        When recursive=True, includes items from all sub-collections of the collection (recursively
        expanding the collection tree). Returns normalized AcademicItems (attachments excluded, and
        carries extra["collections"] = the list of collection keys).
        """
        # 1) Resolve the target set of collection keys. Only when recursive do we need the tree:
        # computing descendants requires the parent->child map, and that map costs a full
        # collection-tree pagination. For a non-recursive listing of one collection's direct members
        # we can skip that entirely (a real saving on a large-library `data members --no-recursive`).
        if recursive:
            nodes = self.collection_tree()
            children: dict[str, list[str]] = {}
            for n in nodes:
                children.setdefault(n["parent"], []).append(n["key"])

            def descendants(root: str) -> set[str]:
                out: set[str] = set()
                stack = [root]
                while stack:
                    k = stack.pop()
                    if k in out:
                        continue
                    out.add(k)
                    stack.extend(children.get(k, []))
                return out

            target = descendants(coll_key)
        else:
            target = {coll_key}

        # 2) Paginate the whole library's items (excluding attachments), attribute locally via the collections field
        collected: list[AcademicItem] = []
        raw = self._paginate(
            f"/{self._users}/items", {"format": "json", "itemType": "-attachment"}
        )
        for it in raw:
            d = it.get("data", {})
            if not d:
                continue
            colls = d.get("collections") or []
            if target & set(colls):
                item = _item_to_academic(d, self.name)
                item.extra["collections"] = colls
                collected.append(item)
        return collected

    def get_attachment(self, key: str) -> bytes:
        # If the key itself is an attachment item (itemType==attachment), take its file directly
        try:
            _, data = self._json_path(f"/{self._users}/items/{key}")
            if data.get("data", {}).get("itemType") == "attachment":
                status, _, raw = self._request("GET", f"/{self._users}/items/{key}/file")
                if status < 400:
                    return raw
        except RuntimeError:
            pass
        # Otherwise, find the first PDF attachment among the parent item's children
        _, children = self._json_path(f"/{self._users}/items/{key}/children", {"format": "json"})
        for ch in children or []:
            d = ch.get("data", {})
            if d.get("itemType") == "attachment" and d.get("contentType") in ("application/pdf", None):
                status, _, raw = self._request("GET", f"/{self._users}/items/{d['key']}/file")
                if status < 400:
                    return raw
        raise RuntimeError(f"[{self.name}] item {key} has no available PDF attachment")

    def add(self, item: AcademicItem) -> str:
        self._require("write")
        version = self._library_version()
        # The local API write body uses a plain JSON array (not the Web API's {"items": [...]})
        payload = [self._to_zotero_json(item)]
        status, headers, raw = self._request(
            "POST", f"/{self._users}/items", body=payload,
            extra_headers={"If-Unmodified-Since-Version": version},
        )
        if status >= 400:
            hint = "; writing needs a local authorization key, run `litcli backends authorize zotero`" if status == 401 else ""
            raise RuntimeError(f"[{self.name}] add -> HTTP {status}{hint}: {raw[:250]!r}")
        resp = json.loads(raw) or {}
        ok = resp.get("successful", {}) or {}
        if ok:
            first = next(iter(ok.values()))
            return first.get("key") or first.get("key", "")
        return item.key

    def patch(self, key: str, fields: dict) -> AcademicItem:
        self._require("write")
        # Use the current item's version as concurrency control
        item = self.get(key)
        version = item.extra.get("version") or self._library_version()
        # The CLI passes high-level fields; map them here to Zotero write JSON camelCase
        zbody = {}
        if "title" in fields:
            zbody["title"] = fields["title"]
        if "abstract" in fields:
            zbody["abstractNote"] = fields["abstract"]
        if "year" in fields:
            zbody["date"] = fields["year"]
        if "doi" in fields:
            zbody["DOI"] = fields["doi"]
        if "url" in fields:
            zbody["url"] = fields["url"]
        status, _, raw = self._request(
            "PATCH", f"/{self._users}/items/{key}", body=zbody,
            extra_headers={"If-Unmodified-Since-Version": str(version)},
        )
        if status >= 400:
            hint = "; writing needs a local authorization key, run `litcli backends authorize zotero`" if status == 401 else ""
            raise RuntimeError(f"[{self.name}] patch {key} -> HTTP {status}{hint}: {raw[:250]!r}")
        return self.get(key)

    def delete(self, key: str) -> None:
        """Delete an item (and its child attachments). Writes to the library; needs an API key."""
        self._require("write")
        version = self._library_version()
        status, _, raw = self._request(
            "DELETE", f"/{self._users}/items/{key}",
            extra_headers={"If-Unmodified-Since-Version": version},
        )
        if status >= 400:
            hint = "; writing needs a local authorization key, run `litcli backends authorize zotero`" if status == 401 else ""
            raise RuntimeError(f"[{self.name}] delete {key} -> HTTP {status}{hint}: {raw[:250]!r}")

    def attach(self, key: str, src: str) -> str:
        """Attach a full-text attachment to an existing item ``key`` (link mode references, does not
        copy the file content).

        src local path -> linked_file, URL -> linked_url. Returns the attachment item key.
        """
        self._require("write")
        is_url = src.startswith(("http://", "https://"))
        link_mode = "linked_url" if is_url else "linked_file"
        att = {
            "itemType": "attachment",
            "parentItem": key,
            "linkMode": link_mode,
            "contentType": "application/pdf",
            "title": os.path.basename(src.split("//")[-1]) if not is_url else "",
            "url": src if is_url else "",
            "path": "" if is_url else src,
        }
        version = self._library_version()
        payload = [att]  # The local API write body uses a plain array
        status, _, raw = self._request(
            "POST", f"/{self._users}/items", body=payload,
            extra_headers={"If-Unmodified-Since-Version": version},
        )
        if status >= 400:
            hint = "; writing needs a local authorization key, run `litcli backends authorize zotero`" if status == 401 else ""
            raise RuntimeError(f"[{self.name}] attach {key} -> HTTP {status}{hint}: {raw[:250]!r}")
        resp = json.loads(raw) or {}
        ok = resp.get("successful", {}) or {}
        if ok:
            return next(iter(ok.values())).get("key", "")
        return ""

    # --- helpers ---

    def _library_version(self) -> str:
        headers, _ = self._json_path(f"/{self._users}/items", {"limit": 1, "format": "json"})
        mv = headers.get("Last-Modified-Version") or headers.get("last-modified-version")
        return str(mv) if mv is not None else "0"

    @staticmethod
    def _to_zotero_json(item: AcademicItem) -> dict:
        # Prefer the original zotero itemType (already stored in extra at parse time); otherwise infer from the CSL type, with article conflicting to journalArticle
        raw_type = item.extra.get("zoteroItemType") if isinstance(item.extra, dict) else None
        if not raw_type:
            csl_to_type = {v: k for k, v in _ITEMTYPE_MAP.items()}
            raw_type = csl_to_type.get(item.item_type, "document")
            if item.item_type == "article" and raw_type != "journalArticle":
                raw_type = "journalArticle"
        creators = [
            {"firstName": c.get("given", ""), "lastName": c.get("family", ""),
             "creatorType": "author"}
            for c in item.creators
        ]
        out: dict = {
            "itemType": raw_type,
            "title": item.title,
            "creators": creators,
            "date": item.year,
        }
        if item.abstract:
            out["abstractNote"] = item.abstract
        if item.container_title:
            out["publicationTitle"] = item.container_title
        if item.volume:
            out["volume"] = item.volume
        if item.issue:
            out["issue"] = item.issue
        if item.page:
            out["pages"] = item.page
        if item.publisher:
            out["publisher"] = item.publisher
        if item.doi:
            out["DOI"] = item.doi
        if item.url:
            out["url"] = item.url
        return out
