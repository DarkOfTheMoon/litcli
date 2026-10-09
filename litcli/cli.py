"""litcli CLI 编排层:argparse 子命令,分发到对应后端。

命令面(后端口经 --backend,默认 zotero;--all 聚合并标来源):
  search  get  add  set  export  pdf  backends
按后端 capabilities 暴露 add/set(只读后端会抛 NotSupportedError)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from . import pdf as pdfutil
from . import metadata
from . import unpaywall
from . import fulltext as fulltext_mod
from .config import get_all_backends, get_backend, load_config
from .models import AcademicItem
from .aggregate import merge_items
from .citation import render_intext, render_bibliography
from .backends.base import NotSupportedError


def _add_common_backend(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", default="zotero", help="后端名(默认 zotero)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litcli", description="通用学术 CLI(可插拔多后端)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="检索")
    p.add_argument("query")
    p.add_argument("--backend", default="zotero")
    p.add_argument("--all", action="store_true", help="对全部后端聚合搜")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--format", choices=["table", "json", "csl"], default="table")
    p.add_argument("--no-cite", action="store_true", help="table 模式不显示 in-text 引文")

    p = sub.add_parser("get", help="单条全字段")
    p.add_argument("key")
    _add_common_backend(p)
    p.add_argument("--format", choices=["table", "json", "csl"], default="table")

    p = sub.add_parser("add", help="新增条目(仅可写后端)")
    p.add_argument("--csl", default=None, help="从 csljson 文件导入(优先于下面各字段)")
    p.add_argument("--title", required=True)
    p.add_argument("--creator", action="append", default=[], help="'家族名, 名字',可多次")
    p.add_argument("--type", default="document")
    p.add_argument("--year", default="")
    p.add_argument("--doi", default="")
    p.add_argument("--url", default="")
    p.add_argument("--abstract", default="")
    _add_common_backend(p)

    p = sub.add_parser("set", help="修改条目字段(仅可写后端)")
    p.add_argument("key")
    p.add_argument("--title", default=None)
    p.add_argument("--abstract", default=None)
    p.add_argument("--year", default=None)
    p.add_argument("--doi", default=None)
    p.add_argument("--url", default=None)
    _add_common_backend(p)

    p = sub.add_parser("export", help="导出引文")
    p.add_argument("key", nargs="+", help="一个或多个条目 key")
    _add_common_backend(p)
    p.add_argument("--format", choices=["csljson", "bibtex", "ris", "text"], default="csljson")

    p = sub.add_parser("pdf", help="取 PDF 附件/全文")
    p.add_argument("key")
    _add_common_backend(p)
    p.add_argument("--text", action="store_true", help="经 pdftotext 抽文本")
    p.add_argument("--out", default=None, help="保存路径")
    p.add_argument("--lines", type=int, default=6, help="--text 预览行数")

    p = sub.add_parser("gen-meta", help="抽取本地 PDF 首页元数据 → csljson")
    p.add_argument("pdf", help="本地 PDF 路径")
    p.add_argument("--out", default=None, help="输出 json 路径(默认 <pdf>.csl.json)")

    p = sub.add_parser("oa", help="Unpaywall 反查合法 OA 全文")
    p.add_argument("doi")
    p.add_argument("--email", default=None)
    p.add_argument("--download", action="store_true", help="下载最佳 OA PDF")
    p.add_argument("--out", default=None, help="下载保存路径")

    p = sub.add_parser("fulltext", help="合法 OA 全文(Unpaywall→OpenAlex,无则标被墙)")
    p.add_argument("doi")
    p.add_argument("--email", default=None)
    p.add_argument("--download", action="store_true", help="下载候选全文 PDF")
    p.add_argument("--out", default=None, help="下载保存路径")
    p.add_argument("--lines", type=int, default=6, help="下载后预览行数")

    sub.add_parser("backends", help="列出已配置后端及能力")

    return parser


def _present(item: AcademicItem, fmt: str = "table", no_cite: bool = False) -> str:
    if fmt == "json":
        return json.dumps(item.to_dict(), ensure_ascii=False)
    if fmt == "csl":
        return json.dumps(item.to_csl(), ensure_ascii=False)
    authors = ", ".join(i.get("family", "") for i in item.creators) or "Anon"
    first = f"{item.source}/{item.key}"
    line = f"{first:<22} {authors:<24} {item.year:<5} {item.title[:60]}"
    if not no_cite:
        cite = render_intext([item])
        line = f"{line}   {cite[0]}"
    return line


def cmd_search(cfg, args) -> int:
    if args.all:
        backends = get_all_backends(cfg)
        items: list[AcademicItem] = []
        for name, b in backends.items():
            try:
                items.extend(b.search(args.query, limit=args.limit))
            except NotImplementedError:
                continue
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                # 单后端失败(未连接/HTTP 错误/超时)不拖垮整个聚合,跳过继续
                print(f"[跳过] 后端 {name}: {e}", file=sys.stderr)
                continue
    else:
        backend = get_backend(args.backend, cfg)
        items = backend.search(args.query, limit=args.limit)

    # 聚合:仅 --all 多条时去重/合并/排序并标来源
    if args.all and items and len(items) > 1:
        items = merge_items(items)

    if args.format in ("json", "csl"):
        for it in items:
            print(_present(it, args.format))
        return 0
    for it in items:
        print(_present(it, "table", args.no_cite))
    if not items:
        print("[无结果]")
    return 0


def cmd_get(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    item = backend.get(args.key)
    print(_present(item, args.format, no_cite=True))
    if args.format == "table" and item.abstract:
        print(f"\n  {item.abstract[:260]}")
    if args.format == "table" and item.attachments:
        print(f"  附件: {', '.join(item.attachments)}")
    return 0


def cmd_add(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
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


def cmd_export(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    items = [backend.get(k) for k in args.key]
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


def cmd_pdf(cfg, args) -> int:
    backend = get_backend(args.backend, cfg)
    raw = backend.get_attachment(args.key)
    if args.text:
        text = pdfutil.extract_text(raw, max_chars=None)
        print(pdfutil.first_lines(text, args.lines))
        return 0
    out = args.out or f"{args.key}.pdf"
    with open(out, "wb") as f:
        f.write(raw)
    print(f"已保存 {len(raw)} 字节 to {out}")
    return 0


def cmd_gen_meta(cfg, args) -> int:
    """抽取本地 PDF 首页元数据 → csljson(供人工校验后 add --csl 导入)。"""
    item = metadata.extract_metadata(args.pdf)
    csl = item.to_csl()
    csl["id"] = item.key or "pdf-meta"
    out = args.out or (os.path.splitext(args.pdf)[0] + ".csl.json")
    with open(out, "w") as f:
        json.dump(csl, f, ensure_ascii=False, indent=2)
    print(f"已写出 csljson: {out}")
    print(json.dumps(csl, ensure_ascii=False, indent=2))
    if not (item.title or item.creators or item.year):
        print("[警告] 未抽取到任何字段,请人工校验后再导入")
    return 0


def cmd_oa(cfg, args) -> int:
    email = (args.email
             or (cfg.get("unpaywall") or {}).get("email")
             or os.environ.get("UNPAYWALL_EMAIL")
             or unpaywall.DEFAULT_EMAIL)
    info = unpaywall.resolve_oa(args.doi, email)
    print(f"DOI {args.doi}  is_oa={info['is_oa']}  status={info['oa_status']}")
    if info.get("title"):
        print(f"  title: {info['title'][:70]}")
    if info.get("best_landing"):
        print(f"  landing: {info['best_landing']}")
    if info.get("best_pdf"):
        print(f"  PDF: {info['best_pdf']}")
        if args.download:
            raw = pdfutil.download(info["best_pdf"])
            out = args.out or f"{args.doi.replace('/', '_')}.pdf"
            with open(out, "wb") as f:
                f.write(raw)
            print(f"  已下载 {len(raw)} 字节 to {out}")
    else:
        print("  (暂无 OA PDF 直链)")
    return 0


def _resolve_oa_email(cfg, args) -> str:
    return (args.email
            or (cfg.get("unpaywall") or {}).get("email")
            or os.environ.get("UNPAYWALL_EMAIL")
            or unpaywall.DEFAULT_EMAIL)


def cmd_fulltext(cfg, args) -> int:
    """合法全文链:本地库 → Unpaywall → OpenAlex;拿不到则指路图书馆/代理。"""
    email = _resolve_oa_email(cfg, args)
    backends = get_all_backends(cfg)
    info = fulltext_mod.resolve_fulltext(args.doi, email, backends=backends)
    if not info.get("has_fulltext"):
        print(f"DOI {args.doi}  本地库与开放 OA 源均无该文献")
        print("  请走机构订阅 / 校际互借 / 配置代理访问;本 CLI 不绕过付费墙。")
        if info.get("errors"):
            print(f"  [解析跳过: {len(info['errors'])} 步]")
        else:
            print("  (各源均未命中,非网络故障)")
        return 0

    title = info.get("title") or ""
    kind = info["kind"]
    print(f"DOI {args.doi}  全文可用 (source={kind})")
    if title:
        print(f"  title: {title[:70]}")

    if kind == "local":
        # 本地库命中:附件已在手,直接落盘(无需 --download)
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

    # OA 源(Unpaywall / OpenAlex):默认只给直链,--download 才真下载
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


def cmd_backends(cfg) -> int:
    for name, b in sorted(get_all_backends(cfg).items()):
        caps = ", ".join(sorted(b.capabilities))
        print(f"{name:<12} caps=[{caps}]")
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
        return {
            "search": cmd_search, "get": cmd_get, "add": cmd_add,
            "set": cmd_set, "export": cmd_export, "pdf": cmd_pdf,
            "gen-meta": cmd_gen_meta,
            "oa": cmd_oa,
            "fulltext": cmd_fulltext,
            "backends": lambda c, a: cmd_backends(c),
        }[args.cmd](cfg, args)
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
