"""litcli CLI 编排层:argparse 意图分族,分发到对应后端。

命令面(后端口经 --backend,默认 zotero):
  search       跨后端检索候选(只读)
  data get/add/set/del/attach   本地库增删改查(写库·须决策)
  fetch fulltext   按 DOI 定位合法全文(只读,不碰库)
  export       导出引文(只读,不碰 PDF)
  backends     诊断
  backends authorize [zotero]   主动向 Zotero 本地授权拿写库 key 并写回 config.toml(写配置)
写库命令先查 backend.can("write"),只读后端抛 NotSupportedError(CLI 捕获为[跳过])。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

from . import pdf as pdfutil
from . import __version__
from . import metadata
from . import unpaywall
from . import fulltext as fulltext_mod
from .config import get_all_backends, get_backend, load_config, write_backend_key
from .models import AcademicItem
from .aggregate import _cited_key, merge_items
from .citation import render_intext, render_bibliography
from .backends.base import NotSupportedError


def _add_common_backend(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", default="zotero", help="后端名(默认 zotero)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litcli", description="通用学术 CLI(可插拔多后端)")
    parser.add_argument("--version", action="version", version=f"litcli {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- 顶层:检索 / 产出 / 诊断 ---
    p = sub.add_parser("search", help="跨后端检索候选(只读)")
    p.add_argument("query")
    p.add_argument("--backend", default="zotero")
    p.add_argument("--all", action="store_true", help="对全部后端聚合搜")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--format", choices=["table", "json", "csl"], default="table")
    p.add_argument("--type", default=None, help="按条目类型过滤(article/book/chapter/patent/...),降噪声")
    p.add_argument("--title", default=None, help="按标题含词收窄(字段级过滤,AND)")
    p.add_argument("--author", default=None, help="按作者姓名收窄,如 Bonaventura")
    p.add_argument("--year", default=None, help="按发表年份收窄,如 2018")
    p.add_argument("--container", default=None, help="按期刊/书名收窄")
    p.add_argument("--sort", choices=["cited"], default=None,
                   help="按被引数降序排序(默认按年份降序)")
    p.add_argument("--no-cite", action="store_true", help="table 模式不显示 in-text 引文")

    for name, help_ in (("refs", "参考文献(bibliography):该文所引用的文献,回溯找奠基"),
                        ("citing", "引用该文的文献(前向追踪,按被引降序)"),
                        ("related", "语义相关文献(OpenAlex related_works,邻居)")):
        pg = sub.add_parser(name, help=help_)
        pg.add_argument("key")
        pg.add_argument("--backend", default="openalex", help="后端(默认 openalex;仅 openalex 实现)")
        pg.add_argument("--limit", type=int, default=5)
        pg.add_argument("--format", choices=["table", "json", "csl"], default="table")
        pg.add_argument("--sort", choices=["cited"], default=None,
                        help="按被引数降序(默认 refs/citing 已按被引降序;此开关对 related 生效)")
        pg.add_argument("--no-cite", action="store_true", help="table 模式不显示 in-text 引文")

    p = sub.add_parser("export", help="导出引文(只读,不碰 PDF)")
    p.add_argument("key", nargs="+", help="一个或多个条目 key")
    _add_common_backend(p)
    p.add_argument("--format", choices=["csljson", "bibtex", "ris", "text"], default="csljson")

    p = sub.add_parser("backends", help="后端诊断 / 授权(试运行前先 `backends authorize` 拿写库 key)")
    bsub = p.add_subparsers(dest="backends_cmd")
    bsub.add_parser("list", help="列出已配置后端与能力(只读)")
    bsub.add_parser("status", help="显示各后端授权/接入状态(只读)")
    bp = bsub.add_parser("authorize", help="主动向 Zotero 本地授权并写回 config.toml")
    bp.add_argument("backend", nargs="?", default="zotero",
                    help="后端名(默认 zotero;目前仅 zotero 支持本地授权)")

    # --- data:本地库增删改查(写库·须决策) ---
    d = sub.add_parser("data", help="本地库操作(增删改查;只读后端会提示)")
    dsub = d.add_subparsers(dest="data_cmd", required=True)

    dp = dsub.add_parser("get", help="单条全字段")
    dp.add_argument("key")
    _add_common_backend(dp)
    dp.add_argument("--format", choices=["table", "json", "csl"], default="table")

    dp = dsub.add_parser("add", help="摄入元信息/元信息+全文")
    dp.add_argument("--csl", default=None, help="从 csljson 文件导入(优先于各字段)")
    dp.add_argument("--pdf", default=None, help="从本地 PDF 抽元数据(启发式,待核对)")
    dp.add_argument("--title", default="")
    dp.add_argument("--creator", action="append", default=[], help="'家族名, 名字',可多次")
    dp.add_argument("--type", default="document")
    dp.add_argument("--year", default="")
    dp.add_argument("--doi", default="")
    dp.add_argument("--url", default="")
    dp.add_argument("--abstract", default="")
    dp.add_argument("--force", action="store_true", help="跳过查重/半残门,直接写")
    _add_common_backend(dp)

    dp = dsub.add_parser("set", help="修改条目字段")
    dp.add_argument("key")
    dp.add_argument("--title", default=None)
    dp.add_argument("--abstract", default=None)
    dp.add_argument("--year", default=None)
    dp.add_argument("--doi", default=None)
    dp.add_argument("--url", default=None)
    _add_common_backend(dp)

    dp = dsub.add_parser("del", help="删除条目")
    dp.add_argument("key")
    _add_common_backend(dp)

    dp = dsub.add_parser("attach", help="给已有条目挂全文附件")
    dp.add_argument("key")
    dp.add_argument("src", help="本地路径或 URL")
    _add_common_backend(dp)

    dp = dsub.add_parser("tree", help="罗列集合树(层级显示;只读)")
    _add_common_backend(dp)

    dp = dsub.add_parser("members", help="罗列某集合的条目(按集合过滤;只读)")
    dp.add_argument("coll_key", help="集合 key(可用 `data tree` 查到)")
    dp.add_argument("--no-recursive", action="store_true", default=False,
                    help="仅该集合本身,不递归包含子集合")
    _add_common_backend(dp)
    dp.add_argument("--format", choices=["table", "json"], default="table")
    dp.add_argument("--limit", type=int, default=200, help="最多罗列条数(0=不限)")

    # --- fetch:取全文/补元数据(只读,不碰库) ---
    f = sub.add_parser("fetch", help="取全文/元数据(只读,不碰库)")
    fsub = f.add_subparsers(dest="fetch_cmd", required=True)

    fp = fsub.add_parser("fulltext", help="按 DOI 定位合法全文(本地库→Unpaywall→OpenAlex)")
    fp.add_argument("doi")
    fp.add_argument("--email", default=None)
    fp.add_argument("--download", action="store_true", help="下载候选全文 PDF")
    fp.add_argument("--out", default=None, help="保存路径")
    fp.add_argument("--lines", type=int, default=6, help="下载后预览行数")

    return parser


def _present(item: AcademicItem, fmt: str = "table", no_cite: bool = False) -> str:
    if fmt == "json":
        return json.dumps(item.to_dict(), ensure_ascii=False)
    if fmt == "csl":
        return json.dumps(item.to_csl(), ensure_ascii=False)
    authors = ", ".join(i.get("family", "") for i in item.creators) or "Anon"
    first = f"{item.source}/{item.key}"
    line = f"{first:<22} {authors:<24} {item.year:<5} {item.title[:60]}"
    if item.cited_by_count:
        line += f"   [被引 {item.cited_by_count}]"
    if not no_cite:
        cite = render_intext([item])
        line = f"{line}   {cite[0]}"
    return line


def _print_items(items: list[AcademicItem], fmt: str, no_cite: bool, empty_msg: str) -> int:
    """Render a list of items (json/csl single-line blocks, or table rows) + ``empty_msg`` fallback."""
    if fmt in ("json", "csl"):
        if not items:
            print("[]")  # machine-readable empty list, not a silent blank stdout
        for it in items:
            print(_present(it, fmt, no_cite))
        return 0
    for it in items:
        print(_present(it, "table", no_cite))
    if not items:
        print(empty_msg)
    return 0


def _search_filters(args) -> dict:
    """Collect the CLI search ``--title/--author/--year/--container`` into a filters dict (non-empty only)."""
    return {k: v for k, v in {
        "title": getattr(args, "title", None),
        "author": getattr(args, "author", None),
        "year": getattr(args, "year", None),
        "container": getattr(args, "container", None),
    }.items() if v}


def cmd_search(cfg, args) -> int:
    filters = _search_filters(args)
    if args.all:
        backends = get_all_backends(cfg)
        items: list[AcademicItem] = []
        for name, b in backends.items():
            try:
                items.extend(b.search(args.query, limit=args.limit, filters=filters))
            except NotImplementedError:
                continue
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                # 单后端失败(未连接/HTTP 错误/超时)不拖垮整个聚合,跳过继续
                print(f"[跳过] 后端 {name}: {e}", file=sys.stderr)
                continue
    else:
        backend = get_backend(args.backend, cfg)
        items = backend.search(args.query, limit=args.limit, filters=filters)

    # 聚合:仅 --all 多条时去重/合并/排序并标来源
    if args.all and items and len(items) > 1:
        items = merge_items(items)

    # --type filter: drop noisy items (book titles / off-topic). Post-merge so --all dedup
    # is preserved; kept as a CLI post-filter for cross-backend semantic consistency.
    if getattr(args, "type", None):
        want = args.type.lower()
        items = [it for it in items if it.item_type.lower() == want]

    # --sort cited: descending by citation count (default is merge's year/title order).
    if getattr(args, "sort", None) == "cited":
        items = sorted(items, key=_cited_key)

    return _print_items(items, args.format, args.no_cite, "[无结果]")


def _normalize_related_key(key: str) -> str:
    """Normalize a ``related`` key: strip a source prefix (openalex/ arxiv/ crossref/ zotero/)
    and turn an arXiv id into its DOI, so the openalex backend's W-id/DOI lookup can resolve
    the key forms the CLI itself emits (``extra["openalex_id"]``, table keys, exported arXiv keys)."""
    k = key.strip()
    for prefix in ("openalex/", "arxiv/", "crossref/", "zotero/"):
        if k.startswith(prefix):
            k = k[len(prefix):]
            break
    aid = _arxiv_id_from_key(k)
    if aid:
        return f"10.48550/arxiv.{aid}"
    return k


_EDGE_METHODS = {"refs": "references", "citing": "citing", "related": "related"}


def _cmd_edge(cfg, args, method: str) -> int:
    """Shared edge-traversal command: dispatch ``method`` (references/citing/related) on one backend."""
    backend = get_backend(args.backend, cfg)
    # Only openalex implements the graph edges; the rest raise NotSupportedError (caught in main).
    # Ordering (by citation count) is owned by the backend, selected here via ``sort_cited``.
    items = getattr(backend, method)(_normalize_related_key(args.key), args.limit,
                                     sort_cited=(args.sort == "cited"))
    return _print_items(items, args.format, args.no_cite, "[无相关]")


def cmd_get(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    item = backend.get(args.key)
    print(_present(item, args.format, no_cite=True))
    if args.format == "table" and item.abstract:
        print(f"\n  {item.abstract[:260]}")
    if args.format == "table" and item.attachments:
        print(f"  附件: {', '.join(item.attachments)}")
    return 0


def cmd_data_add(cfg, args) -> int:
    """摄入元信息(data add --title.. / --csl)或元信息+全文(data add --pdf)。

    元数据唯一写入口:--pdf 时用启发式抽候选、展示待核对,用户确认/-force 才写。
    写前过 should_import 查重/半残门,防本地库爆库。
    """
    backend = get_backend(args.backend, cfg)
    item = None
    if args.pdf:
        item = metadata.extract_metadata(args.pdf)
        item.source = args.backend
        print("--- 从 PDF 首页启发式抽取(待核对) ---")
        print(f"  title: {item.title}")
        print(f"  authors: {', '.join(a.get('family','') for a in item.creators) or '-'}")
        print(f"  year: {item.year or '-'}")
        if item.abstract:
            print(f"  abstract: {item.abstract[:120]}")
        if not (item.title or item.creators or item.year):
            print("[警告] 未抽取到任何字段,建议 --csl 或手填,勿盲目写库")
    else:
        creators = []
        for c in args.creator:
            parts = [x.strip() for x in c.split(",")]
            fam = parts[0]
            giv = parts[1] if len(parts) > 1 else ""
            creators.append({"family": fam, "given": giv})
        item = AcademicItem(
            source=args.backend, title=args.title, creators=creators, year=args.year,
            doi=args.doi, url=args.url, abstract=args.abstract, item_type=args.type,
        )
        if args.csl:
            import json as _json
            with open(args.csl, encoding="utf-8") as f:
                csl = _json.load(f)
            item = AcademicItem.from_csl(csl, source=args.backend)

    # 查重/半残门(仅 --force 跳过)
    if not args.force:
        try:
            existing = backend.search(item.doi or item.title, limit=20)
        except Exception:
            existing = []
        ok, reason = fulltext_mod.should_import(item, existing)
        if not ok:
            print(f"[跳过] {reason}")
            print("  若要强制收录,加 --force")
            return 2

    key = backend.add(item)
    print(f"已新增 [{args.backend}] key={key}")
    return 0


def cmd_set(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    fields = {k: v for k, v in {
        "title": args.title, "abstract": args.abstract, "year": args.year,
        "doi": args.doi, "url": args.url,
    }.items() if v is not None}
    it = backend.patch(args.key, fields)
    print(f"已更新 [{args.backend}] {args.key}")
    print(_present(it, "table", no_cite=True))
    return 0


def _arxiv_id_from_key(key: str) -> str | None:
    """从导出 key 中识别 arXiv id(如 ``arxiv/1806.09180`` 或 ``arXiv:1806.09180``)。"""
    m = re.match(r"^\s*(?:arxiv[:/]|arXiv[:/]|arXiv)?\s*(\d{4}\.\d{4,5})(?:v\d+)?\s*$", key)
    if m:
        return m.group(1)
    return None


def _normalize_export_key(key: str, backend_name: str) -> str:
    """导出前把 arXiv key 归一为后端可解析的形态。

    - arxiv 后端:直接给裸 arXiv id(其 get 走 ``id:<key>`` 检索);
    - 其它后端(openalex/crossref):转为 DOI ``10.48550/arxiv.<id>``,否则
      openalex.get 会把 ``arxiv/1806.09180`` 当 OpenAlex id 查而 404。
    """
    aid = _arxiv_id_from_key(key)
    if not aid:
        return key
    if backend_name == "arxiv":
        return aid
    return f"10.48550/arxiv.{aid}"


def cmd_export(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    items = [backend.get(_normalize_export_key(k, args.backend)) for k in args.key]
    if args.format == "csljson":
        if len(items) == 1:
            print(json.dumps(items[0].to_csl(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps([i.to_csl() for i in items], ensure_ascii=False, indent=2))
    elif args.format == "bibtex":
        for it in items:
            print(it.to_bibtex())
    elif args.format == "ris":
        for it in items:
            print(it.to_ris())
    else:
        for it in items:
            print(render_intext([it])[0])
            print(render_bibliography([it])[0])
    return 0


def cmd_data_del(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    backend.delete(args.key)
    print(f"已删除 [{args.backend}] {args.key}")
    return 0


def cmd_data_attach(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    att_key = backend.attach(args.key, args.src)
    print(f"已挂全文附件 [{args.backend}] {args.key} 附件key={att_key or '-'} src={args.src}")
    return 0


def _collection_path_map(backend) -> dict:
    """由 backend.collection_tree() 建 {key: "父 / 子 / 孙"} 路径映射。"""
    nodes = backend.collection_tree()
    name = {n["key"]: n["name"] for n in nodes}
    parent = {n["key"]: n["parent"] for n in nodes}
    out = {}
    for k in name:
        parts, cur, seen = [], k, set()
        while cur and cur in name and cur not in seen:
            seen.add(cur)
            parts.insert(0, name[cur])
            cur = parent.get(cur)
        out[k] = " / ".join(parts)
    return out


def _render_tree(nodes: list[dict]) -> str:
    """把集合节点列表渲染为层级缩进树文本。"""
    parent = {n["key"]: n["parent"] for n in nodes}
    name = {n["key"]: n["name"] for n in nodes}
    roots = [n["key"] for n in nodes if not parent.get(n["key"])]

    def depth(k: str) -> int:
        d, c = 0, k
        while c and c in parent:
            c = parent.get(c)
            d += 1
        return d

    lines = []
    seen = set()

    def walk(key: str, prefix: str, is_last: bool):
        if key in seen or not name.get(key):
            return
        seen.add(key)
        if depth(key) > 0:
            connector = "└─ " if is_last else "├─ "
            lines.append(f"  {prefix}{connector}{name[key]}  ({key})")
        else:
            lines.append(f"  {name[key]}  ({key})")
        child_keys = sorted([n["key"] for n in nodes if parent.get(n["key"]) == key],
                            key=lambda k: name[k].lower())
        child_prefix = prefix + ("   " if is_last else "│  ")
        for i, ck in enumerate(child_keys):
            walk(ck, child_prefix, i == len(child_keys) - 1)

    roots.sort(key=lambda k: name[k].lower())
    for i, rk in enumerate(roots):
        walk(rk, "", i == len(roots) - 1)
    return "\n".join(lines)


def cmd_data_tree(cfg, args) -> int:
    """罗列集合树(层级 indent 显示)。只读。"""
    backend = get_backend(args.backend, cfg)
    nodes = backend.collection_tree()
    if not nodes:
        print("[无集合]")
        return 0
    print(_render_tree(nodes))
    return 0


def cmd_data_members(cfg, args) -> int:
    """罗列某集合(默认递归全部子集合)的条目。只读。"""
    backend = get_backend(args.backend, cfg)
    recursive = not getattr(args, "no_recursive", False)
    items = backend.collection_items(args.coll_key, recursive=recursive)

    if args.format == "json":
        for it in (items[: args.limit] if args.limit else items):
            print(json.dumps(it.to_dict(), ensure_ascii=False))
        return 0

    path_map = _collection_path_map(backend)
    sel = path_map.get(args.coll_key, args.coll_key)
    shown = items[: args.limit] if args.limit else items
    note = f"(共 {len(items)} 条,出示前 {len(shown)} 条)" if args.limit and len(items) > len(shown) else ""
    print(f"[{sel}] 命中 {len(items)} 条条目 {note}")
    for it in shown:
        colls = it.extra.get("collections", []) or []
        subs = ", ".join(path_map.get(c, c) for c in colls
                         if path_map.get(c, c) and path_map.get(c, c) != sel) or "-"
        try:
            cite = render_intext([it])[0]
        except Exception:
            cite = ""
        authors = ", ".join(i.get("family", "") for i in it.creators) or "Anon"
        print(f"  {it.key:<8} {authors[:22]:<22} {it.year:<5} {it.title[:50]:<50} | {subs[:46]}  {cite}")
    return 0


def _resolve_oa_email(cfg, args) -> str:
    return (args.email
            or (cfg.get("unpaywall") or {}).get("email")
            or os.environ.get("UNPAYWALL_EMAIL")
            or unpaywall.DEFAULT_EMAIL)


def cmd_fetch_fulltext(cfg, args) -> int:
    """按 DOI 定位合法全文:本地库(优先)→ Unpaywall → OpenAlex;均无则指路。"""
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    info = fulltext_mod.resolve_fulltext(args.doi, email, backends=backends)
    if not info.get("has_fulltext"):
        print(f"DOI {args.doi}  本地库与开放 OA 源均无该文献")
        print("  请走机构订阅 / 校际互借 / 配置代理访问;本 CLI 不绕过付费墙。")
        if info.get("errors"):
            print(f"  [解析跳过: {len(info['errors'])} 步]")
        return 0

    title = info.get("title") or ""
    kind = info["kind"]
    print(f"DOI {args.doi}  全文可用 (source={kind})")
    if title:
        print(f"  title: {title[:70]}")

    if kind == "local":  # 本地库命中:附件已在手,直接落盘
        key = info.get("key") or ""
        src = info.get("source") or "zotero"
        print(f"  本地库 [{src}] 附件已命中 (key={key})")
        raw = info["data"]
        out = args.out or f"{args.doi.replace('/', '_')}.pdf"
        with open(out, "wb") as f:
            f.write(raw)
        print(f"  已保存 {len(raw)} 字节 to {out}")
        if raw and args.lines:
            text = pdfutil.extract_text(raw, max_chars=None)
            print(f"  --- {args.lines} 行预览 ---\n{pdfutil.first_lines(text, args.lines)}")
        return 0

    # OA 源:默认只给直链,--download 才真下载
    if info.get("status"):
        print(f"  oa_status: {info['status']}")
    print(f"  PDF: {info['url']}")
    if args.download:
        raw = pdfutil.download(info["url"])
        out = args.out or f"{args.doi.replace('/', '_')}.pdf"
        with open(out, "wb") as f:
            f.write(raw)
        print(f"  已下载 {len(raw)} 字节 to {out}")
        if raw and args.lines:
            text = pdfutil.extract_text(raw, max_chars=None)
            print(f"  --- {args.lines} 行预览 ---\n{pdfutil.first_lines(text, args.lines)}")
    return 0


def cmd_backends(cfg, args) -> int:
    subcmd = getattr(args, "backends_cmd", None)
    if subcmd == "authorize":
        return cmd_backends_authorize(cfg, getattr(args, "backend", "zotero"))
    if subcmd == "status":
        return cmd_backends_status(cfg, args)
    # 裸 `backends` 或 `backends list`:列出已配置后端与能力(只读)
    for name, b in sorted(get_all_backends(cfg).items()):
        caps = ", ".join(sorted(b.capabilities))
        print(f"{name:<12} caps=[{caps}]")
    return 0


def _cfg_write_key(cfg: dict, name: str) -> str | None:
    """取 config 里某后端的 api_key(只判有无,不打印明文)。"""
    bcfg = (cfg.get("backends") or {}).get(name) or {}
    k = bcfg.get("api_key")
    return k if k else None


def cmd_backends_status(cfg, args) -> int:
    """各后端只读诊断:配置/能力/授权与可达性(不打印任何 key 明文)。"""
    backends = get_all_backends(cfg)
    if not backends:
        print("[无后端] 请检查 ~/.config/litcli/config.toml (或 env LITCLI_CONFIG)")
        return 0
    print(f"后端诊断: {len(backends)} 个已配置")
    for name, b in sorted(backends.items()):
        caps = ", ".join(sorted(b.capabilities))
        has_write = "write" in b.capabilities
        if has_write:
            auth = "已授权(有写 key)" if _cfg_write_key(cfg, name) else "未授权(写库需 backends authorize)"
        else:
            auth = "只读后端,无需授权"
        # 仅对提供了 ping 的后端(zotero 本地)做轻量可达性探测,失败不致命
        probe = ""
        ping = getattr(b, "ping", None)
        if callable(ping):
            probe = "  可达" if ping() else "  [不可达:后端未运行?]"
        print(f"  {name:<12} caps=[{caps}]  {auth}{probe}")
    return 0


def cmd_backends_authorize(cfg, backend: str) -> int:
    """主动向 Zotero 本地授权拿 key,并写回 config.toml。

    被授权后端需支持本地授权(实现 authorize())。成功(或已授权)后写库不再弹框。
    """
    try:
        b = get_backend(backend, cfg)
    except KeyError as e:
        print(f"[错误] {e}")
        return 2
    if not hasattr(b, "authorize"):
        print(f"[错误] 后端 '{backend}' 不支持本地授权(仅 zotero_local 提供)")
        return 2
    try:
        key = b.authorize()
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 2
    if not key:
        print(f"[错误] {backend} 授权未返回 key(可能内建 key 已被占用,或未批准)")
        return 2
    path = write_backend_key(backend, key)
    print(f"[ok] 已写入 {backend} 的写库 key 到 {path}")
    print("     后续写库(data add / set / del / attach)无需再授权。")
    return 0


def _apply_proxy(cfg: dict) -> None:
    """config 配了 [network].proxy 且环境变量未设代理时,统一走该代理(校园/VPN 访问海外源)。"""
    prog = (cfg.get("network") or {}).get("proxy")
    if not prog:
        return
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return  # 环境变量已配置,优先
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": prog, "https": prog})))


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_config()
    _apply_proxy(cfg)
    try:
        if args.cmd == "search":
            return cmd_search(cfg, args)
        if args.cmd in _EDGE_METHODS:
            return _cmd_edge(cfg, args, _EDGE_METHODS[args.cmd])
        if args.cmd == "export":
            return cmd_export(cfg, args)
        if args.cmd == "backends":
            return cmd_backends(cfg, args)
        if args.cmd == "data":
            return {
                "get": cmd_get, "add": cmd_data_add,
                "set": cmd_set, "del": cmd_data_del,
                "attach": cmd_data_attach,
                "tree": cmd_data_tree, "members": cmd_data_members,
            }[args.data_cmd](cfg, args)
        if args.cmd == "fetch":
            return {
                "fulltext": cmd_fetch_fulltext,
            }[args.fetch_cmd](cfg, args)
        print(f"[错误] 未知命令 {args.cmd}")
        return 2
    except NotSupportedError as e:
        print(f"[跳过] {e}")
        return 2
    except KeyError as e:
        print(f"[错误] {e}")
        return 2
    except RuntimeError as e:
        # 后端业务错误(HTTP 401/404/超时 等)优雅给出提示,而非堆栈
        print(f"[错误] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
