---
name: zotero
description: 与用户学术资料互操作——检索、读取、导出引文、读 PDF 全文、新增/修改。覆盖本机 Zotero 库与 arXiv。当用户提到"我的文献/论文库/引用/参考文献/PDF"时使用。
---

# Zotero / arXiv 学术互操作

零依赖工具 `litcli`，后端无关：领域模型统一为 `AcademicItem`，Zotero 本地库可读可写，arXiv 联网只读。

## 命令面（`litcli` 已在 PATH，或在 `~/Projects/Scholar/litcli` 下用 `./bin/litcli`）

后端经 `--backend`（默认 `zotero`），`--all` 跨后端聚合（去重/合并/排序，`extra["sources"]` 标来源）。
命令按意图分族：`search`/`fetch`/`export` 只读，`data` 写库（须决策）。

| 任务 | 命令 | 读写 |
|---|---|---|
| 检索候选 | `litcli search "关键词" [--all] [--limit N]` | 只读 |
| 单条全字段 | `litcli data get <key> [--format csl\|json]` | 只读 |
| 导出引文 | `litcli export <key> [--format csljson\|bibtex\|ris\|text]` | 只读 |
| 定位合法全文 | `litcli fetch fulltext <doi> [--download]` 本地→OA→指路(绕不过付费墙) | 只读 |
| **新增(元信息)** | `litcli data add --title "..." --creator "家族, 名字" --year 2002 [--doi ...]` | **写库** |
| **新增(从 PDF)** | `litcli data add --pdf <file>` 抽启发式候选→展示→人工核对后写 | **写库** |
| 修改 | `litcli data set <key> --title "..." [--year ...]` | **写库** |
| 删除 | `litcli data del <key>` | **写库** |
| 挂全文附件 | `litcli data attach <key> <本地路径\|URL>` | **写库** |
| 罗列集合树 | `litcli data tree` 层级显示全部集合 | 只读 |
| 罗列某集合条目 | `litcli data members <集合key> [--no-recursive]` 默认递归全部子集合 | 只读 |
| 列后端 | `litcli backends` | 只读 |
| **首次写库授权** | `litcli backends authorize zotero` 弹框批准后写回 config | 写配置 |

## 环境事实

- Zotero 10 本机运行于 `localhost:23119`，本地 API 免 key（`users/0` 别名→真实库）；写请求需 `Zotero-Server-ID` + `If-Unmodified-Since-Version`（工具已自动处理）。
- **写库前先授权一次**：`litcli backends authorize zotero` → 本地弹框批准 → key 写入 `[backends.zotero].api_key`，此后写操作自动携带、不再弹框。未授权时 `data set/del/attach` 报 `HTTP 401`（退出码 2）。
- Better BibTeX 未装：引文走 `export --format csljson` 喂 `pandoc --citeproc`，不依赖 BBT。
- 读 PDF 全文需系统 `pdftotext`；只读后端（arxiv/crossref/openalex/uspto）的 `data` 写操作报 `[跳过]`。

## 注意

- `data add` 返回新 key；写库仅对可写后端（zotero）成功，其余报错并退出码 2。
- `data add` 写前过 `should_import` 查重/半残门：同 DOI/题已存在、或缺标题/缺作者且缺年份 → 拒绝，需 `--force` 覆盖。
- `data add --pdf` 是**启发式**候选，结果请人工核对后再写，勿盲目入库。
- 跨后端聚合字段对齐用 `--all`；无 `--all` 默认仅 zotero。
