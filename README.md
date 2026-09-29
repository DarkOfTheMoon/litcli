# litcli — 通用学术 CLI(零依赖,多后端可插拔)

一条导学文献在**多个后端**（本机 Zotero / arXiv）间的**读到写**，统一归一化为一个领域模型。
面向 Claude Code 代理与你的学术资料互操作：检索、读取全文、新增/修改、导出引文。

- **零第三方依赖**：仅 Python 3.11+ 标准库（`urllib` / `tomllib` / `xml.etree` / `subprocess`）。
- **后端无关的领域层**（`models` / `citation` / `pdf` / `metadata` / `aggregate`），可作独立库复用。
- **可插拔后端**：`backends/` 定义抽象 `Backend` + capabilities；本期实现 Zotero（本地 API，读+写+附件）与 arXiv（联网，只读）。

## 安装 / 运行

无打包、无 `pip install` 步骤（`pyproject.toml` 仅为元数据/可选 `.local` 入口）：

```bash
# 方式一:项目内直接运行(无需安装)
./bin/litcli search "drop"

# 方式二(可选):软链到 ~/.local/bin,全局可用
./install.sh          # 同时把 skill 复制到 ~/.claude/skills/zotero
litcli backends
```

PDF 全文抽取需系统有 `pdftotext`(poppler-utils)；无则 `pdf --text` 走空，其余功能不受影响。

## 命令

子命令（后端经 `--backend`，默认 `zotero`；`--all` 跨后端聚合并标来源）：

| 命令 | 说明 |
|---|---|
| `litcli search "X" [--all] [--limit N] [--format table\|json\|csl]` | 检索；`--all` 对各后端聚合，**去重/合并/排序**，`extra["sources"]` 标全部来源 |
| `litcli get <id> [--backend NAME]` | 单条全字段 |
| `litcli add --title "..." [--creator "家族, 名字"]... [--year ..]` | 仅可写后端（zotero）；只读后端报 `[跳过]` 退出码 2 |
| `litcli set <id> --title .. [--abstract ..] [--year ..]` | 同仅可写后端 |
| `litcli export <id> [--format csljson\|bibtex\|text]` | 导出引文；csljson 可喂 `pandoc --citeproc`，**不依赖 Better BibTeX** |
| `litcli pdf <id> [--text] [--out PATH]` | 取附件/直链 PDF；`--text` 经 pdftotext 抽取 |
| `litcli gen-meta <pdf> [--out X.json]` | 从本地 PDF 首页**离线启发式**抽元数据 → csljson（人工校验后 `add --csl` 导入） |
| `litcli backends` | 列出已配置后端及能力 |

## 配置

`~/.config/litcli/config.toml`（不支持则读 `$LITCLI_CONFIG`；无任何配置则用内置默认=仅本地 Zotero）：

```toml
[backends.zotero]
type = "zotero_local"
base = "http://localhost:23119/api"     # 本机 Zotero 10 本地 API
# 写(POST/PATCH/DELETE)需真实 Zotero API key;localhost key 仅可读
# api_key = "你的-zotero-api-key"

[backends.arxiv]
type = "arxiv"
```

## 架构

```
litcli/
├── models.py      AcademicItem 归一化模型 + ⇄ CSL JSON / BibTeX
├── citation.py    零依赖 author-date in-text / 参考文献渲染
├── pdf.py         下载 + pdftotext 抽文本(后端无关)
├── metadata.py    PDF 首页启发式元数据(纯函数,离线)
├── aggregate.py   跨后端聚合:去重/合并/排序(纯函数)
├── config.py      配置读取、后端发现、get_backend/get_all_backends
├── cli.py         argparse 编排,按 capabilities 暴露读写
├── backends/       base.py(Backend ABC) + zotero_local.py + arxiv.py
└── tests/          unittest 单测(stub 后端,离线)
```

分层原则：**领域层**（任何后端无关，纯函数优先）→ **后端层**（可插拔，只读/写/附件由 capabilities 标注）→ **配置层** → **CLI 编排层**。

## 测试

```bash
python3 -m unittest discover -s tests
```

用 stub 服务器 / mock urlopen 离线跑（不连真库、不联网）：覆盖归一化、聚合去重/合并/排序、Zotero 写入请求形状（含 `Zotero-Server-ID` / `If-Unmodified-Since-Version`）、arXiv 请求构造/Atom 解析/错误包装。

## 已知限制

- Zotero 写落库需**真实 Zotero API key**（本地 localhost key 仅为只读）：在 Zotero 偏好设置生成 key，写入 config 的 `api_key`。缺 key 时 `add`/`set` 报 `HTTP 401;写入需真实 Zotero API key`（退出码 2），不污染库；配好 key 后 `litcli add` 即可真正落库。
- arXiv 联网在部分环境（如受限沙箱）返回 406；代码已把 HTTP 错误包装为 `RuntimeError`，`--all` 会跳过失败后端继续。
- `citation.render_bibliography` 在无作者且无年份的极端条目下会因 `it.author` 属性缺失而崩——已知缺陷见 `citation.py`。
