# litcli — 通用学术 CLI(零依赖,多后端可插拔)

一条导学文献在**多个后端**（本机 Zotero / arXiv）间的**读到写**，统一归一化为一个领域模型。
面向 Claude Code 代理与你的学术资料互操作：检索、读取全文、新增/修改、导出引文。

- **零第三方依赖**：仅 Python 3.11+ 标准库（`urllib` / `tomllib` / `xml.etree` / `subprocess`）。
- **后端无关的领域层**（`models` / `citation` / `pdf` / `metadata` / `aggregate`），可作独立库复用。
- **可插拔后端**：`backends/` 定义抽象 `Backend` + capabilities；本期实现 Zotero（本地 API，读+写+附件）与 arXiv（联网，只读）。

## 安装 / 运行

零第三方依赖，标准 Python 包安装（`pyproject.toml` 声明入口点 `litcli = "litcli.cli:main"`）。本机 Python 3.12 是 PEP 668（externally-managed）环境、默认拦系统/用户 pip 安装，`install.sh` 因此用 `pip install --break-system-packages --user .`（**非 editable**）：安装时把源码**复制**为冻结快照到用户站点（`~/.local`），console_script 落 `~/.local/bin/litcli`（已在 PATH）。生产 `litcli` 与仓库源码**解耦**——生产期间改源码**不会**波及已装版本（改代码不会导致生产出错）；发布新版本时重跑安装即可。零第三方依赖（纯标准库）故 `--break-system-packages` 无与 apt 包冲突之虞，`--user` 限定用户作用域、免 sudo。开发在仓库内走 `./bin/litcli`（独立用源码，互不影响生产）：

```bash
# 生产方式(推荐):`pip install --break-system-packages --user .`(复制快照,无 venv)
./install.sh          # 同时复制 skill 到 ~/.claude/skills/zotero
litcli backends       # 自测:列出已配置后端及能力

# 开发方式:仓库内直接运行(免安装,任意 cwd 可用;与生产快照隔离)
./bin/litcli search "drop"
```

PDF 全文抽取需系统有 `pdftotext`(poppler-utils)；无则 `pdf --text` 走空，其余功能不受影响。

## 命令

子命令（后端经 `--backend`，默认 `zotero`；`--all` 跨后端聚合并标来源）。命令按意图分族，`search`/`fetch`/`export` 只读，`data` 写库（须决策）：

| 命令 | 说明 | 读写 |
|---|---|---|
| `litcli search "X" [--all] [--limit N] [--type article] [--title ..] [--author ..] [--year ..] [--container ..] [--sort cited] [--format table\|json\|csl]` | 检索；`--all` 对各后端聚合，**去重/合并/排序**，`extra["sources"]` 标全部来源；`--type` 按条目类型过滤降噪；`--title/--author/--year/--container` 字段级过滤(与 query AND 叠加,各后端映射见下)；`--sort cited` 按被引数降序，table 显示 `[被引 N]` | 只读 |
| `litcli related <key> [--limit N] [--backend openalex]` | **相关文献**：OpenAlex `related_works` 图谱(读父 work 内联 id 逐个取全量)；不支持的只读后端输出 `[跳过]` 退出码 2 | 只读 |
| `litcli data get <id> [--format csl\|json]` | 单条全字段 | 只读 |
| `litcli data add --title "..." [--creator "家族, 名字"]... [--year ..]` | 摄入元信息；写前过 `should_import` 查重/半残门，只读后端报 `[跳过]` | **写库** |
| `litcli data add --pdf <file>` | 从 PDF 首页**启发式**抽候选，展示待核对后才写；不自动入库 | **写库** |
| `litcli data set <id> --title .. [--abstract ..] [--year ..]` | 修改字段 | **写库** |
| `litcli data del <id>` | 删除条目 | **写库** |
| `litcli data attach <id> <本地路径\|URL>` | 给已有条目挂全文附件 | **写库** |
| `litcli data tree` | 罗列集合树(层级显示全部集合) | 只读 |
| `litcli data members <collKey> [--no-recursive]` | 罗列某集合条目,默认递归全部子集合;按每条 `collections` 字段本地归属 | 只读 |
| `litcli export <id> [--format csljson\|bibtex\|ris\|text]` | 导出引文；csljson 可喂 `pandoc --citeproc`，**不依赖 Better BibTeX**（不碰 PDF）；含期刊名/卷/期/页/出版社；arXiv key 如 `arxiv/1806.09180` 自动归一为 DOI | 只读 |
| `litcli fetch fulltext <doi> [--download] [--lines N]` | **合法全文定位**：本地库 → Unpaywall → OpenAlex OA 直链；都拿不到则**指路图书馆/代理**（不绕过付费墙） | 只读 |
| `litcli backends list` / `status` | 列出已配置后端及能力 / 显示各后端**授权与可达状态** | 只读 |
| `litcli backends authorize [zotero]` | 主动向 Zotero **本地授权**拿写库 key 并写回 `config.toml`（`[backends.zotero].api_key`）；此后 `data add/set/del/attach` 免再授权 | 写配置 |

`search` 的 `--title/--author/--year/--container` 在各后端映射如下（后端无对应字段→尽力折叠进自由查询词或忽略）：

- **crossref** → `query.title` / `query.author` / `query.container-title` / `filter=from-pub-date:YYYY-01-01,until-pub-date:YYYY-12-31`（与 `query.bibliographic` 并存自动 AND）。
- **openalex** → `filter=title.search:` / `raw_author_name.search:` / `publication_year:`；`container` 折叠进自由 `search`（`primary_location.source.display_name.search` 实测 400，非硬过滤）。
- **arxiv** → `ti:` / `au:` / `submittedDate:[YYYY01010000 TO YYYY12312359]`，用 ` AND ` 拼 `search_query`。
- **zotero** → 仅单 `q`，把各字段词折进 q（配合 `qmode=everything` 全字段命中）；`--type` 保持后置过滤（跨后端语义统一）。

被引数 `cited_by_count`：openalex `cited_by_count`、crossref `is-referenced-by-count`；`search` table 显示 `[被引 N]`，`--sort cited` 按被引降序。

## 配置

`~/.config/litcli/config.toml`（不支持则读 `$LITCLI_CONFIG`；无任何配置则用内置默认=Zotero+arXiv+crossref+openalex）：

```toml
[backends.zotero]
type = "zotero_local"
base = "http://localhost:23119/api"     # 本机 Zotero 10 本地 API
# 写(POST/PATCH/DELETE)需本地授权 key。运行 `litcli backends authorize zotero`,
# 在 Zotero 本地弹框批准后自动写入下一行;无需手动生成(zotero.org 的 Web API key 不适用)。
# api_key = "本地授权-key(backends authorize 自动写入)"

[backends.arxiv]
type = "arxiv"

# 可选:专利检索(USPTO,key 门控,免费注册)
[backends.uspto]
type = "uspto"
# api_key = "你的-uspto-api-key"

[network]
# proxy = "http://127.0.0.1:7890"   # 校园/VPN 访问海外源;默认读 HTTP_PROXY / HTTPS_PROXY
```

## 架构

```
litcli/
├── models.py      AcademicItem 归一化模型 + ⇄ CSL JSON / BibTeX
├── citation.py    零依赖 author-date in-text / 参考文献渲染
├── pdf.py         下载 + pdftotext 抽文本(后端无关)
├── metadata.py    PDF 首页启发式元数据(纯函数,离线)
├── aggregate.py   跨后端聚合:去重/合并/排序(纯函数)
├── fulltext.py    合法全文定位(本地→OA)+ should_import 收录查重门(纯函数)
├── config.py      配置读取、后端发现、get_backend/get_all_backends
├── cli.py         argparse 意图分族编排(search/related/data/fetch/export),按 capabilities 暴露读写
├── backends/       base.py(Backend ABC) + zotero_local.py + arxiv.py
│                   + crossref.py + openalex.py + openlibrary.py + uspto.py
└── tests/          unittest 单测(stub 后端,离线)
```

分层原则：**领域层**（任何后端无关，纯函数优先）→ **后端层**（可插拔，只读/写/附件由 capabilities 标注）→ **配置层** → **CLI 编排层**。

## 测试

```bash
python3 -m unittest discover -s tests
```

用 stub 服务器 / mock urlopen 离线跑（不连真库、不联网）：覆盖归一化、聚合去重/合并/排序、Zotero 写入请求形状（含 `Zotero-Server-ID` / `If-Unmodified-Since-Version`）、arXiv 请求构造/Atom 解析/错误包装。

## 已知限制

- Zotero **写库**（`data add/set/del/attach`）需一个写权限的 key：先跑 `litcli backends authorize zotero`，它经 Zotero 本地 API 的 `/api/local/authorize` 对你弹一次授权框，批准后把本地 key 写回 `config.toml` 的 `[backends.zotero].api_key`，此后写操作自动携带该 key、不再弹框。**无需**在 Zotero 偏好设置手动生成 key（Zotero 10 本地 API 用运行时授权派发本地 key，非 zotero.org 的 Web API key）。未授权时写操作报 `HTTP 401`（退出码 2），不污染库。
- arXiv 联网在部分环境（如受限沙箱）返回 406；代码已把 HTTP 错误包装为 `RuntimeError`，`--all` 会跳过失败后端继续。
- **专利检索（USPTO）需真实 API key**：免 key 可达的 Crossref/OpenAlex 公开版专利记录趋近 0，专利检索以 USPTO 为主通道。免费注册 key（`https://developer.uspto.gov` 顶部 “Get a developer key”）后写入 config 的 `api_key`；缺 key 时 `search --backend uspto` 明确提示而非堆栈。
- **软件著作权（软著）无稳定公开 API**：中国版权保护中心登记检索需账号/验证码，无法自动化批量检索；暂不内置软著后端，属计划外缺口。
- **Sci-Hub / ZLibrary 类盗版全文源不内置**：版权规避/盗版分发平台，刻意排除；合法全文走 `litcli fetch fulltext`（本地库→Unpaywall→OpenAlex 的开放获取版，绕不过付费墙则指路机构/馆际互借）。
