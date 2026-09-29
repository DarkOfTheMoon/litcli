"""Zotero 本地库后端(Zotero 10,localhost:23119 本地 API)。

能力:读 + 写(get_attachment).本地 API 无需 key;统一带 Zotero-API-Version: 3。
本机用户库用 ``users/0`` 别名(响应会展开为真实 library id)。写入用
If-Unmodified-Since-Version 并发控制头。
"""
from __future__ import annotations

import json
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

# Zotero 写入 JSON 的字段名(camelCase)与 AcademicItem 的对应
_ZOTERO_RESPONSE_FIELDS = ("version", "key")


def first_digit_year(text: str) -> str:
    """从 Zotero 的 date 字段提取 4 位年份,如 '2002' / '2020-03-12' -> '2002'。"""
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
    """Zotero item.data JSON -> AcademicItem。"""
    creators = [p for p in (_parse_creator(c) for c in data.get("creators", [])) if p]
    item_type = _ITEMTYPE_MAP.get(data.get("itemType", ""), "document")
    raw_date = data.get("date", "")
    return AcademicItem(
        source=source,
        key=data.get("key", ""),
        title=data.get("title", ""),
        creators=creators,
        year=first_digit_year(raw_date),
        abstract=data.get("abstractNote", ""),
        doi=data.get("DOI", ""),
        url=data.get("url", ""),
        item_type=item_type,
        extra={"date": raw_date, "zoteroItemType": data.get("itemType", "")},
    )


class ZoteroLocalBackend(Backend):
    def __init__(self, name: str = "zotero", base: str = "http://localhost:23119/api",
                 library: str = "0", api_key: str | None = None):
        self.name = name
        self._base = base.rstrip("/")
        self._users = f"users/{library or '0'}"
        self._api_key = api_key
        self._server_id_val: str | None = None

    @property
    def capabilities(self) -> set[str]:
        return {"read", "write", "attachment"}

    # --- 底层请求 ---

    def _url(self, path: str) -> str:
        return f"{self._base}/{path.lstrip('/')}"

    def _headers(self) -> dict:
        # 读可伪装 localhost;真正写(POST/PATCH/DELETE)需真实 API key,否则 401。
        return {
            "Zotero-API-Version": "3",
            "Zotero-API-Key": self._api_key or "localhost",
            "Content-Type": "application/json",
        }

    def _server_id(self) -> str:
        """Zotero 10 本地写请求需 Zotero-Server-ID(防跨实例)。

        该值来自 **/api 请求的响应头**(而非 /connector/ping 的 body,后者是
        HTML "Zotero is running"),每个 /api 响应都携带。惰性发一个极小 GET,
        把其响应头里的 Server-ID 缓存到 _server_id_val(也在 _request 里缓存)。

        《注意》:缓存字段名 _server_id_val 与实例方法 _server_id 分开——若同名
        (如 self._server_id = None),会在方法体内把 self._server_id 解析成 bound
        method,导致读取永不执行、并返回方法对象而非字符串。
        """
        if not self._server_id_val:
            try:
                self._json_path(f"/{self._users}/items", {"limit": 1, "format": "json"})
            except Exception:
                self._server_id_val = ""
        return self._server_id_val

    def _cache_server_id(self, hdr: dict) -> None:
        """从 /api 响应头缓存 Zotero-Server-ID(写请求回传需要)。"""
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
        # 写操作(POST/PATCH/DELETE,或带 body)需 Zotero-Server-ID
        if method != "GET" or body is not None:
            sid = self._server_id()
            if sid:
                headers.setdefault("Zotero-Server-ID", sid)
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

    # --- Backend 接口 ---

    def search(self, query: str, limit: int = 10) -> list[AcademicItem]:
        headers, data = self._json_path(f"/{self._users}/items", {"q": query, "limit": limit, "format": "json"})
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

    def get_attachment(self, key: str) -> bytes:
        # 若 key 本身是附件条目(itemType==attachment),直接取其 file
        try:
            _, data = self._json_path(f"/{self._users}/items/{key}")
            if data.get("data", {}).get("itemType") == "attachment":
                status, _, raw = self._request("GET", f"/{self._users}/items/{key}/file")
                if status < 400:
                    return raw
        except RuntimeError:
            pass
        # 否则从父条目的 children 中找第一个 PDF 附件
        _, children = self._json_path(f"/{self._users}/items/{key}/children", {"format": "json"})
        for ch in children or []:
            d = ch.get("data", {})
            if d.get("itemType") == "attachment" and d.get("contentType") in ("application/pdf", None):
                status, _, raw = self._request("GET", f"/{self._users}/items/{d['key']}/file")
                if status < 400:
                    return raw
        raise RuntimeError(f"[{self.name}] 条目 {key} 无可用 PDF 附件")

    def add(self, item: AcademicItem) -> str:
        self._require("write")
        version = self._library_version()
        payload = {"items": [self._to_zotero_json(item)]}
        status, headers, raw = self._request(
            "POST", f"/{self._users}/items", body=payload,
            extra_headers={"If-Unmodified-Since-Version": version},
        )
        if status >= 400:
            hint = ";写入需真实 Zotero API key:请在 Zotero 偏好设置生成 key 并写入 config 的 api_key" if status == 401 else ""
            raise RuntimeError(f"[{self.name}] add -> HTTP {status}{hint}: {raw[:250]!r}")
        resp = json.loads(raw) or {}
        ok = resp.get("successful", {}) or {}
        if ok:
            first = next(iter(ok.values()))
            return first.get("key") or first.get("key", "")
        return item.key

    def patch(self, key: str, fields: dict) -> AcademicItem:
        self._require("write")
        # 用当前 item 的 version 作并发控制
        item = self.get(key)
        version = item.extra.get("version") or self._library_version()
        # CLI 传高层字段,这里映射为 Zotero 写入 JSON 的 camelCase
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
            hint = ";写入需真实 Zotero API key:请在 Zotero 偏好设置生成 key 并写入 config 的 api_key" if status == 401 else ""
            raise RuntimeError(f"[{self.name}] patch {key} -> HTTP {status}{hint}: {raw[:250]!r}")
        return self.get(key)

    # --- helpers ---

    def _library_version(self) -> str:
        headers, _ = self._json_path(f"/{self._users}/items", {"limit": 1, "format": "json"})
        mv = headers.get("Last-Modified-Version") or headers.get("last-modified-version")
        return str(mv) if mv is not None else "0"

    @staticmethod
    def _to_zotero_json(item: AcademicItem) -> dict:
        # 优先用原始 zotero itemType(解析时已存 extra);否则按 csl 类型推断,article 冲突取 journalArticle
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
        if item.doi:
            out["DOI"] = item.doi
        if item.url:
            out["url"] = item.url
        return out
