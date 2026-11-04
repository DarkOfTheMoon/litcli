# litcli — 通用学术 CLI（多后端可插拔）

同一条学术文献（期刊论文或图书），在多个后端之间统一读写：本机 Zotero、arXiv、Crossref、OpenAlex，可选 OpenLibrary、USPTO、CNKI（知网题录）、Douban（豆瓣图书），全部归一化为一个领域模型。它面向 Claude Code 代理与个人学术资料库互操作——检索、读全文、新增/修改、导出引文。

- **用成熟的轮子，不重造网络层**：`httpx` 负责连接池 / HTTP/2 / 代理 / 超时，`tenacity` 负责 429 与 5xx 退避，`pyrate-limiter` 负责按站点限速，`lxml` 负责 HTML/XML 解析。
- **后端无关领域层**：`models` / `citation` / `pdf` / `metadata` / `aggregate`，可独立复用。
- **可插拔后端**：`backends/` 定义抽象 `Backend` 与 `capabilities`。默认启用 Zotero、arXiv、Crossref、OpenAlex；OpenLibrary、USPTO、CNKI、Douban 按需在配置中启用（CNKI/Douban 需额外装 Playwright）。

## 安装 / 运行

标准 Python 包安装：`pyproject.toml` 入口点 `litcli = "litcli.cli:main"`。本机 Python 3.12 是 PEP 668（externally-managed）环境，`install.sh` 用 `pip install --break-system-packages --user .`（非 editable）。安装时把源码复制为冻结快照放到 `~/.local`，生产 `litcli` 与仓库源码解耦：生产期间改源码不波及已装版本，发布新版本时重跑安装即可。依赖与程序一起装进用户站点（`--user`），不动系统包、免 sudo。开发直接用 `./bin/litcli`，与生产快照互不影响。

```bash
# 生产方式：./install.sh = pip install --break-system-packages --user .(复制快照，无 venv)
./install.sh          # 顺带分发 agent 技能(见下)
litcli backends       # 自测：列出已配置后端及能力

# 开发方式：仓库内直接跑，任意 cwd 可用，与生产快照隔离
./bin/litcli search "drop"
```

PDF 全文抽取依赖系统 `pdftotext`（poppler-utils）；缺失时返回空文本，其余功能不受影响。

### 技能分发（不绑定某个 agent）

技能源是 `skills/litcli/SKILL.md`（纯 markdown）。`install.sh` 默认**自动探测**已存在的 agent 配置目录，只往探测到的那些装，格式各随其主流：

| 目标 | 落点 |
|---|---|
| Claude Code | `~/.claude/skills/litcli/SKILL.md`（含 frontmatter） |
| Codex CLI | `~/.codex/prompts/litcli.md`（剥离 frontmatter 的 prompt） |
| Cursor | `~/.cursor/rules/litcli.mdc`（换 Cursor 规则头） |
| Gemini CLI | `~/.gemini/GEMINI.md` 的标记块（重跑替换该块，不动你的其它内容） |

任何情况下都会在 `~/.local/share/litcli/skill/SKILL.md` 留一份中立副本，表中没有的 agent 可直接引用它。用 `./install.sh --skills claude,codex` 显式指定、`--no-skills` 跳过、`--skills-only` 只动技能（不重装包）。

## 命令

子命令按意图分族。后端经 `--backend`（默认 `zotero`）；`--all` 跨后端聚合、去重/合并/排序并在 `extra["sources"]` 标注来源。`search`/`fetch`/`export` 只读，`data` 写库（须决策）。

| 命令 | 说明 | 读写 |
|---|---|---|
| `litcli search "X" [--all] [--limit N] [--type TYPE] [--title ..] [--author ..] [--year ..] [--container ..] [--sort cited] [--format table\|json\|csl]` | 检索候选。`--all` 对各后端聚合去重；`--type` 按条目类型过滤；`--title`/`--author`/`--year`/`--container` 字段级过滤（与 query AND 叠加，映射见下）；`--sort cited` 按被引数降序，table 显示 `[cited N]` | 只读 |
| `litcli refs <key> [--limit N] [--sort cited] [--backend openalex]` | 该文所引文献（OpenAlex `referenced_works`，并行拉取，默认按被引降序） | 只读 |
| `litcli citing <key> [--limit N] [--sort cited] [--backend openalex]` | 引用该文的文献（`filter=cites:<W-id>` 单请求，默认按被引降序） | 只读 |
| `litcli related <key> [--limit N] [--sort cited] [--backend openalex]` | 语义相关文献（OpenAlex `related_works` 邻居）；不支持的后端输出 `[skipped]`，退出码 2 | 只读 |
| `litcli data get <key> [--format table\|json\|csl]` | 单条全字段 | 只读 |
| `litcli data add --title "..." [--creator "家族, 名字"]... [--year ..]` | 摄入元信息；写前过 `should_import` 查重/半残门；不支持的后端报 `[skipped]` | **写库** |
| `litcli data add --pdf <file>` | 从 PDF 首页**启发式**抽候选，展示后经交互确认（y/N）才写库；`--yes` 跳过确认 | **写库** |
| `litcli data set <key> --title .. [--abstract ..] [--year ..]` | 修改字段 | **写库** |
| `litcli data del <key>` | 删除条目 | **写库** |
| `litcli data attach <key> <本地路径\|URL>` | 给已有条目挂全文附件 | **写库** |
| `litcli data tree` | 罗列集合树（层级显示） | 只读 |
| `litcli data members <collKey> [--no-recursive]` | 罗列某集合条目，默认递归全部子集合 | 只读 |
| `litcli export <key>... [--format csljson\|bibtex\|ris\|text]` | 导出引文；`csljson` 可喂 `pandoc --citeproc`，不依赖 Better BibTeX；arXiv key 如 `arxiv/1806.09180` 自动归一为 DOI | 只读 |
| `litcli fetch fulltext <doi> [--scihub] [--download] [--out ..] [--lines N]` | 全文定位：本地库 → Unpaywall → OpenAlex → Europe PMC（OA JATS XML）→ Zenodo（开放文件）→ Unpaywall 落地页；`--scihub` 再兜底盗版镜像（标注来源）；都没有则指路机构订阅/馆际互借 | 只读 |
| `litcli fetch book "书名\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--limit N] [--pick N] [--download] [--out ..]` | 图书定位：检索出候选书单 → 选定一本补全书目 → 可选下载文件。默认 Z-Library（影子图书馆、非 OA 授权、匿名每日下载有配额）；换 `--backend doab` 走开放获取书源（部分记录直链 PDF，另有部分只给出版社落地页，`--download` 会明确报“不是 PDF”）；`--backend openlibrary` 仅书目、无文件 | 只读 |
| `litcli backends list` / `status` | 列出已配置后端及能力 / 显示各后端授权与可达状态 | 只读 |
| `litcli backends authorize [zotero]` | 主动向 Zotero 本地授权拿写库 key 并写回 `config.toml` | 写配置 |
| `litcli completions <bash\|zsh\|fish>` | 打印自包含补全脚本；`eval "$(litcli completions bash)"` 后 `litcli <TAB>` 补子命令/参数/选项取值/文件路径。补全时不起 `litcli` 子进程（spec 内嵌于脚本），CLI 面更新后重新 eval | 只读 |
| `litcli search --backend douban "书词" [--limit N]` | 豆瓣图书**候选**（书名/作者/出版社/年份/评分，需 `install.sh --with-douban`）；`get` 补全完整书目（ISBN/出版社/版次/出版年/页数） | 只读 |

### 检索字段级过滤（各后端映射）

`--title` / `--author` / `--year` / `--container` 在不同后端映射如下；后端没有对应字段时尽量折叠进自由查询词或忽略。

- **crossref** → `query.title` / `query.author` / `query.container-title` / `filter=from-pub-date:YYYY-01-01,until-pub-date:YYYY-12-31`（与 `query.bibliographic` 并存，自动 AND）。
- **openalex** → `filter=title.search:` / `raw_author_name.search:` / `publication_year:`；`container` 折叠进自由 `search`（`primary_location.source.display_name.search` 实测 400，非硬过滤）。
- **arxiv** → `ti:` / `au:` / `submittedDate:[YYYY01010000 TO YYYY12312359]`，用 ` AND ` 拼 `search_query`。按 IP 限速，连发可能 `HTTP 429 Rate exceeded.`，已按 2s/4s 退避重试；可配 `[backends.arxiv].mailto` 加入礼貌池。
- **zotero** → 仅单 `q`，把字段词折进 `q`（配合 `qmode=everything` 全字段命中）；`--type` 保持后置过滤（跨后端语义统一）。

被引数 `cited_by_count`：openalex `cited_by_count`、crossref `is-referenced-by-count`；`search` table 显示 `[cited N]`，`--sort cited` 按被引降序。

## 后端与能力

默认可用的四个后端；其余（OpenLibrary、USPTO、CNKI、豆瓣、Sci-Hub、Z-Library、DOAJ、Europe PMC、DOAB、Zenodo）需在配置中显式启用（均可单次用 `--backend` 指定，Sci-Hub 用 `--scihub`）。

| 后端 | 能力 | 说明 |
|---|---|---|
| `zotero` | read / write / attachment | Zotero 10 本地 API（`localhost:23119/api`），免 zotero.org 的 Web API key；写库需一次运行时授权 |
| `arxiv` | read / attachment | 开放 Atom API；可解析 PDF 直链，按 arXiv id（去 v 版本）归一化；按 IP 限速（429 退避重试 + 可配 mailto） |
| `crossref` | read | DOI 开放元数据；5xx 指数退避重试 |
| `openalex` | read | 学术图谱；摘要为倒排索引需重建；唯一实现引文图谱（refs/citing/related） |
| `openlibrary` | read | 开放图书 `search.json`；默认不内置，显式启用 |
| `uspto` | read | USPTO 专利全文检索；**需免费 API key**（免 key 的 Crossref/OpenAlex 公开版专利记录趋近 0） |
| `cnki` | read | 知网中文题录检索（需 Playwright + 可见浏览器）；只取公开题录元数据，不抓付费墙全文 |
| `douban` | read | 豆瓣图书检索（需 Playwright，默认无头）；`search` 出候选（书名/作者/出版社/年份/评分），`get` 补全 ISBN/版次/页数；只取公开书目元数据 |
| `doaj` | read | DOAJ 开放获取期刊论文索引（全部 OA）；免 key，`--all` 里补齐 OA 覆盖 |
| `europepmc` | read | Europe PMC 生物医学题录 + 开放获取 JATS XML 全文；免 key，`fetch fulltext` 合法链自动咨询 |
| `doab` | read | DOAB 开放获取图书：Atom feed 检索 + 详情页补全（ISBN/出版社/页数/文件）；免 key。详情页有“你不是机器人”工作量证明，由本地 `hashlib` 解出 |
| `zenodo` | read | Zenodo 开放记录（数据集/软件/预印本等），文件可直接下载；免 key，`fetch fulltext` 合法链自动咨询 |
| `scihub` | read | Sci-Hub 全文兜底，只按 DOI 解析 PDF（无关键词检索）；`fetch fulltext --scihub` 才启用，ALTCHA 工作量证明由本地 `hashlib` 解出；盗版镜像，非 OA 授权 |
| `zlibrary` | read | Z-Library 图书检索 + 下载（影子图书馆）；`fetch book` 出候选→补全书目→可选下载；浏览器工作量证明由本地 `hashlib` 解出，入口域名自动解析 |

## 配置

`~/.config/litcli/config.toml`（不支持则读 `$LITCLI_CONFIG`；无任何配置时用内置默认 = zotero + arxiv + crossref + openalex）。

```toml
[backends.zotero]
type = "zotero_local"
base = "http://localhost:23119/api"     # 本机 Zotero 10 本地 API
# 写（POST/PATCH/DELETE）需本地授权 key；运行 `litcli backends authorize zotero`，
# 在 Zotero 本地弹框批准后自动写入下一行。
# api_key = "本地授权-key"

[backends.arxiv]
type = "arxiv"

# 可选：专利检索（USPTO，key 门控，免费注册）
[backends.uspto]
type = "uspto"
# api_key = "你的-uspto-api-key"

[network]
# proxy = "http://127.0.0.1:7890"   # 校园/VPN 访问海外源；默认读 HTTP_PROXY / HTTPS_PROXY
```

## 架构

```
litcli/
├── models.py      AcademicItem 归一化模型 + ⇄ CSL JSON / BibTeX
├── citation.py    author-date in-text / 参考文献简版渲染（纯函数）
├── pdf.py         下载 + pdftotext 抽文本（后端无关）
├── metadata.py    PDF 首页启发式元数据（纯函数，离线）
├── aggregate.py   跨后端聚合：去重/合并/排序（纯函数）
├── fulltext.py    全文定位（本地→OA→可选 Sci-Hub）+ should_import 收录查重门（纯函数）
├── config.py      配置读取、后端发现、get_backend/get_all_backends
├── transport.py   httpx 传输原语：配置的代理、client 工厂、HttpError、cookie 会话
├── cli.py         入口：解析、代理、分发、统一异常映射
├── commands/      parser.py（argparse 面）+ render/keys 共用 + search/graph/data/export/fetch/diagnostics 各族命令
├── backends/      http.py（HttpBackend）+ base.py（Backend ABC）+ zotero_local / arxiv / crossref / openalex / openlibrary / uspto / cnki / douban / doaj / europepmc / doab / zenodo / scihub / zlibrary
└── tests/          unittest 单测（stub 后端，离线）

skills/litcli/      agent 技能源（install.sh 按目标 agent 分发）
```

分层原则：**领域层**（后端无关，纯函数优先）→ **传输层**（代理 / 会话 / 重试，领域层与后端层共用）→ **后端层**（可插拔，读/写/附件由 capabilities 标注）→ **配置层** → **CLI 编排层**（入口只管分发，命令按意图分族在 `commands/`）。

## 测试

```bash
python3 -m unittest discover -s tests
```

用 stub 服务器 / `httpx.MockTransport` 离线跑（不连真库、不联网）：覆盖归一化、聚合去重/合并/排序、Zotero 写入请求形状（含 `Zotero-Server-ID` / `If-Unmodified-Since-Version`）、arXiv 请求构造与 Atom 解析、错误包装。

## 已知限制

- Zotero **写库**（`data add/set/del/attach`）需一个写权限 key。先跑 `litcli backends authorize zotero`，经本地 API `/api/local/authorize` 弹框批准后，key 自动写回 `config.toml` 的 `[backends.zotero].api_key`，此后写操作自动携带、不再弹框。key 是 Zotero 10 本地 API 在运行时授权派发的本地 key，不是 zotero.org 的 Web API key，不需要在 Zotero 设置里手动生成。未授权时写操作报 `HTTP 401`（退出码 2），不污染库。
- arXiv 联网在部分环境（如受限沙箱）返回 406；代码已把 HTTP 错误包装为 `RuntimeError`，`--all` 会跳过失败后端继续。
- **专利检索（USPTO）需真实 API key**：免 key 可达的 Crossref/OpenAlex 公开版专利记录趋近 0，专利以 USPTO 为主通道。免费注册 key（<https://developer.uspto.gov> 顶部 “Get a developer key”）后写入 config 的 `api_key`；缺 key 时 `search --backend uspto` 明确提示而非堆栈。
- **开放获取语料源只取公开 OA 记录**：DOAJ（OA 期刊）、DOAB（OA 图书）、Europe PMC（生物医学题录 + OA JATS XML 全文）、Zenodo（开放记录与文件）都是免 key 的开放授权来源，`fetch fulltext` 只在合法 OA 渠道内使用它们。Europe PMC XML 是**文本而非排版 PDF**，默认存 `.xml`；Unpaywall 只给落地页时会退到链末（输出 `HTML` 并提示浏览器打开）。
- **站点反爬墙：挡的是“无 JS 的客户端”，不是某条 IP**：`europepmc.org` 网页在 Cloudflare **managed challenge + Turnstile** 后面（`cf-mitigated: challenge`）：简单 HTTP 客户端 403，带全套 Chrome 请求头/HTTP2 仍 403，无头与有头 Chromium 也基本卡在 “Just a moment…”（偶发通过），因此 Europe PMC 的检索与全文都走 `www.ebi.ac.uk` 的开放 REST，链接按标识符拼。代价是非 OA 但 `inEPMC=Y` 的记录 REST 的 `fullTextXML` 直接 500，正文只剩被墙的页面。DOAB 用 Anubis：`/rest/*` 对无 JS 客户端直接 403（改走 `/open-search` 的 Atom feed），而 `/handle/*` 是**可解的**工作量证明（`sha256(randomData+n)` 前缀若干十六进制零），litcli 用本地 `hashlib` 解一次、换 cookie 后续用，不绕过。`backends status` 里这类站点显示为可达，不必怀疑自己的 IP 被封。
- **Sci-Hub 为可选兜底，不默认介入**：合法全文仍先走 `litcli fetch fulltext` 的本地库 → Unpaywall → OpenAlex → Europe PMC → Zenodo；加 `--scihub`（或在配置里启用 `[backends.scihub]`）才在合法渠道全无结果后再试盗版镜像，输出会标注 `source=scihub` 与“非 OA 授权”。镜像用 ALTCHA 工作量证明验人（不绕过），每次调用单个 DOI、低频。
- **Z-Library 为影子图书馆**：`litcli fetch book` 走 Z-Library 图书检索 + 下载，非 OA 授权，只取公开书目元数据与公开 PDF 直链，不绕过任何登录墙/验证码。站点以“Checking your browser”工作量证明验人，litcli 用本地 `hashlib` 解同一道题（不绕过）；入口域名会轮换，实现从 302 自动解析真实前端主机。**匿名每日下载有配额**，超限会明确提示（可在 `[backends.zlibrary]` 填 `email`/`password` 登录后放宽）。
- **CNKI（知网）后端只取公开题录，不抓全文**：知网无开放 API，`search --backend cnki` 驱动**可见浏览器**（复用你自己的登录会话）检索，从结果列表页提取标题/作者/刊名/年份/摘要片段。**不实现任何下载/抓取付费墙全文的路径**；遇滑块验证码交给你在可见浏览器手动完成，程序不绕过。需先 `./install.sh --with-cnki` 装 Playwright + Chromium，首次搜索会弹浏览器请你手动过滑块 + 登录一次，之后会话复用。
