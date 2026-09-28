---
name: zotero
description: 用 litcli 命令与本地学术库(Zotero / arXiv)互操作。当用户想检索文献、添加/修改条目、导出引文、读 PDF 全文、或从本地 PDF 抽取元数据时触发。可替代 Zotero MCP——零常驻进程、按需加载省上下文。
---

# academic-lib 互操作(litcli)

用 `litcli`(已装到 PATH;或 `python3 -m litcli.cli`)操作本机 Zotero 10 库与 arXiv。
后端经 `--backend`(默认 zotero),`--all` 聚合;`litcli backends` 查看可用后端及能力。

## 能力矩阵

| 目的 | 命令 |
|---|---|
| 检索(读) | `litcli search "<词>" [--backend zotero\|arxiv] [--all] [--limit N] [--format table\|json\|csl]` |
| 单条全字段 | `litcli get <key> [--backend]` |
| 新增(写) | `litcli add --title "..." --creator "家族, 名字" --year 2020 [--doi ...] [--url ...] [--abstract ...]` |
| 新增(从 csljson) | `litcli add --csl <file.json> [--backend zotero]`(gen-meta 输出) |
| 修改(写) | `litcli set <key> --title 新标题 [--abstract ...] [--year ...] [--doi ...] [--url ...]` |
| 导出引文 | `litcli export <key> [--format csljson\|bibtex\|text]` |
| 读 PDF 全文 | `litcli pdf <key> [--text [--lines N]] [--out x.pdf]` |
| 本地 PDF 首页元数据 | `litcli gen-meta <pdf路径> [--out x.json]`(→ csljson) |
| 后端列表 | `litcli backends` |

## 关键要点

- **后端默认 zotero**:本机库 `localhost:23119/api`,**无需 API key、无需开关**。`users/0` 是本机别名。
- **写能力**:仅带 `write` 能力的后端能 `add`/`set`(zotero);arXiv 只读,会报"该后端不支持操作"。
- **引文兜底**:`export --format csljson` 输出可喂 `pandoc --citeproc` 生成引文与参考文献表;**不依赖 Better BibTeX**。
- **PDF 全文**:经系统 `pdftotext`;`--text` 打印前几行。
- **gen-meta**:启发式抽 PDF 首页 title/authors/year/abstract,结果需**人工校验**后再 `add --csl` 导入。
- 聚合 `--all` 对各后端各搜并**标注来源**,去重/排序暂不做。
