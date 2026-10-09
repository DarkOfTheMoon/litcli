---
name: zotero
description: 与用户学术资料互操作——检索、读取、导出引文、读 PDF 全文、新增/修改。覆盖本机 Zotero 库与 arXiv。当用户提到"我的文献/论文库/引用/参考文献/PDF"时使用。
---

# Zotero / arXiv 学术互操作

零依赖工具 `litcli`，后端无关：领域模型统一为 `AcademicItem`，Zotero 本地库可读可写，arXiv 联网只读。

## 命令面（`litcli` 已在 PATH，或在 `~/Projects/Scholar/litcli` 下用 `./bin/litcli`）

后端经 `--backend`（默认 `zotero`），`--all` 跨后端聚合（去重/合并/排序，`extra["sources"]` 标来源）。

| 任务 | 命令 |
|---|---|
| 检索 | `litcli search "关键词" [--all] [--limit N]` |
| 单条全字段 | `litcli get <key> [--backend ...]` |
| 导出引文 | `litcli export <key> [--format csljson\|bibtex\|text]` |
| 读 PDF 全文 | `litcli pdf <key> --text` |
| 找合法全文 | `litcli fulltext <doi> [--download]` 本地库→OA→指路(绕不过付费墙) |
| 新增 | `litcli add --title "..." --creator "家族, 名字" --year 2002 [--doi ...]` |
| 修改 | `litcli set <key> --title "..." [--year ...]` |
| 本地 PDF→元数据 | `litcli gen-meta <pdf> [--out X.csl.json]` |
| 列后端 | `litcli backends` |

## 环境事实

- Zotero 10 本机运行于 `localhost:23119`，本地 API 免 key（`users/0` 别名→真实库）；写请求需 `Zotero-Server-ID` + `If-Unmodified-Since-Version`（工具已自动处理）。
- Better BibTeX 未装：引文走 `export --format csljson` 喂 `pandoc --citeproc`，不依赖 BBT。
- 读 PDF 全文需系统 `pdftotext`；arXiv 为只读后端，add/set 会报 `[跳过]`。

## 注意

- `add` 返回新 key；`add`/`set` 仅对可写后端（zotero）成功，其余报错并退出码 2。
- 跨后端聚合字段对齐用 `--all`；无 `--all` 默认仅 zotero。
- 从 PDF 首页抽元数据的 `gen-meta` 是**启发式**，结果请人工校验后再导入，勿直接写库。
