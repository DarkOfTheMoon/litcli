# litcli — 学术文献的检索、引文与全文 CLI

[简体中文](README.md) · [English](README.en.md)

一条文献在多个来源之间用同一个模型读写：本机 Zotero 库、arXiv、Crossref、OpenAlex、知网题录、豆瓣图书，
以及 DOAJ / Europe PMC / DOAB / Zenodo 等开放获取语料。方向是给**你和你的 agent** 用——检索、读全文、
导出可喂 `pandoc --citeproc` 的引文、写回你自己的库。

- **不需要 Better BibTeX**：`export --format csljson` 直接产生 CSL JSON，`pandoc --citeproc` 能读。
- **不需要 zotero.org 账号**：走 Zotero 10 的本地 API（`localhost:23119`），写库一次授权即可。
- **没有 Zotero 也能用**：`--backend crossref` 等公网源单独就能检索、取全文、导引文。
- **不绕过验证码**：需要人机验证的站点（知网滑块、Sci-Hub、Z-Library、DOAB）由本地 `hashlib` 做题或交你手点，
  命中盗版源时输出会明确标注“非 OA 授权”。

## 60 秒上手（不需要 Zotero）

```bash
python3 -m venv .venv && . .venv/bin/activate              # 或用你已有的环境
pip install .                                             # Debian/Ubuntu 的 PEP 668 环境改用 ./install.sh
litcli search --backend doaj "graph neural network" --limit 5   # OA 语料，key 就是 DOI
litcli data get 10.1038/s41598-024-80509-9 --backend crossref --format json
litcli export 10.1038/s41598-024-80509-9 --backend crossref --format csljson
litcli fetch fulltext 10.1038/s41598-024-80509-9 --download
```

`--format json` 是一行一条记录；空结果打印 `[]`，可直接喂 `jq`。`--download` 默认写到当前目录，`--out DIR` 另指。

## 安装

```bash
# 方式一：装进当前环境（venv / 任意 Python 3.11+）
pip install .

# 方式二：生产快照（Debian/Ubuntu 的 PEP 668 环境）
./install.sh                 # = pip install --break-system-packages --user .，复制快照到 ~/.local，不动系统包
litcli backends              # 自测：列出已配置后端及能力（PATH 里没有 ~/.local/bin 时见末尾提示）

# 方式三：从发布版本装（发布页附 SHA256SUMS）
git clone --branch v0.1.0 --depth 1 https://gitee.com/simult/litcli.git litcli-0.1.0
cd litcli-0.1.0 && ./install.sh
```

`install.sh` 装的是**源码副本快照**：生产期间改仓库不波及已装版本，发新版本重跑即可。开发时在仓库内直接用
`./bin/litcli`，与已装版本互不影响。PDF 文本抽取依赖系统 `pdftotext`（poppler-utils）；缺失时返回空文本，
其余功能不受影响。CNKI / 豆瓣另需 `./install.sh --with-cnki`（或 `--with-douban`）装浏览器依赖。

升级就是重跑 `./install.sh`（覆盖快照）；卸载用 `pip uninstall litcli` 并删 `~/.config/litcli` 与 `~/.local/share/litcli`。

数据都留在本机：引文写进你自己的 Zotero 库，配置在 `~/.config/litcli/config.toml`，其余只有对外 API 请求。
`install.sh` 还会把 `skills/litcli/SKILL.md` 分发给探测到的 agent（Claude Code / Codex / Cursor / Gemini，
格式各随其主流），并总留一份中立副本在 `~/.local/share/litcli/skill/SKILL.md`；`--no-skills` / `--skills-only`
可跳过包部分或只装技能。

## 命令

`--backend` 选来源（默认 `zotero`）；`--all` 在**配置里启用的**后端之间聚合、去重合并并在 `extra["sources"]` 标注来源。
表格第二列就是 key；查 Zotero 时取去掉 `source/` 前缀的部分，公网后端两种写法都认。
退出码：`0` 成功（含无结果），`2` 出错或后端不支持。
`search` / `fetch` / `export` / `refs` / `citing` / `related` 只读，`data` 写库。

**检索与引文图谱**

| 命令 | 说明 |
|---|---|
| `litcli search "X" [--all] [--backend B] [--type T] [--limit N]` | 检索候选（`--format table\|json\|csl`）；字段过滤与排序见下节 |
| `litcli refs <key>` / `citing <key>` / `related <key>`（均可 `--limit N`、`--sort cited`） | 参考文献（回溯奠基）／引用该文的文献（前向跟进）／语义邻居，均为 OpenAlex 图谱 |
| `litcli data get <key> [--format table\|json\|csl]` | 单条全字段 |
| `litcli export <key>... [--format csljson\|bibtex\|ris\|text] [--no-cite]` | 导出引文；`csljson` 可喂 `pandoc --citeproc`。arXiv key 只在 `--backend openalex\|crossref` 下归一为 DOI |
| `litcli fetch fulltext <doi> [--download] [--out DIR] [--scihub]` | 全文定位：本地库 → Unpaywall → OpenAlex → Europe PMC → Zenodo → 落地页 |
| `litcli fetch book "书名\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--pick N] [--download]` | 图书候选 → 选定补全书目 → 可选下载 |

**写库**（默认 Zotero；只读后端会报 `[skipped]` 并以退出码 2 结束）

| 命令 | 说明 |
|---|---|
| `litcli backends authorize zotero` | 一次性授权写库 key（本地弹框批准后写回配置） |
| `litcli data add --title "..." --creator "家族, 名字" [--year ..] [--doi ..]` | 摄入元信息；写前过查重 / 半残门，`--force` 跳过 |
| `litcli data add --pdf <file>` / `--csl <file>` | 从 PDF 首页启发式抽候选（交互确认 y/N 后写库，`--yes` 跳过）或从 CSL JSON 摄入 |
| `litcli data set <key> --title ..` · `data del <key>` · `data attach <key> <路径\|URL>` | 改 / 删 / 挂全文 |

**诊断**

| 命令 | 说明 |
|---|---|
| `litcli backends list` · `backends status` | 已配置后端与能力 / 授权与可达状态 |
| `litcli completions <bash\|zsh\|fish>` | 打印自包含补全脚本；`eval "$(...)"` 后按 TAB 补全 |

`data tree` / `data members <集合key>` 罗列集合；`--year` 等过滤在各后端的映射见下节。

### 过滤条件在各后端的映射

后端没有对应字段时，尽量折进自由查询词，否则忽略。

- **crossref** → `query.title` / `query.author` / `query.container-title` / `filter=from-pub-date,until-pub-date`
- **openalex** → `filter=title.search:` / `raw_author_name.search:` / `publication_year:`；`container` 折进自由查询
- **arxiv** → `ti:` / `au:` / `submittedDate:[…]`，用 ` AND ` 拼 `search_query`
- **zotero** → 全部折进单个 `q`（`qmode=everything` 全字段命中）

`cited_by_count` 取 openalex / crossref 的对应字段，table 输出显示 `[cited N]`。

## 后端与授权边界

默认启用 Zotero、arXiv、Crossref、OpenAlex。其余随时可用单次 `--backend` 直接指定（不必写配置）；
只有想让它们进入 `--all` 聚合或 `fetch` 默认链，才需要在 `config.toml` 里启用。CNKI / 豆瓣另需浏览器依赖。

| 后端 | 能力 | 授权 | 取什么 |
|---|---|---|---|
| `zotero` | 读 / 写 / 附件 | 你的本机库 | 本地 API，写库一次授权 |
| `arxiv` · `crossref` · `openalex` | 读（arxiv 另给 PDF 直链） | 开放，免 key | 元数据；arxiv / openalex 按 IP 限速并已退避 |
| `doaj` · `europepmc` · `doab` · `zenodo` | 读 | 开放获取（OA），免 key | 公开 OA 记录；europepmc 给 JATS XML、zenodo 给开放文件 |
| `openlibrary` · `uspto` | 读 | 开放（uspto 需免费 key） | 图书题录 / 专利全文；默认不内置 |
| `cnki` | 读 | 知网网页 | 公开**题录**，不抓付费墙全文；需可见浏览器、人工过滑块 |
| `douban` | 读 | 豆瓣网页 | 公开**书目**（书名/作者/ISBN/版次/页数），默认无头 |
| `scihub` | 读 | 盗版镜像（非 OA），只按 DOI | PDF；`--scihub` 才兜底 |
| `zlibrary` | 读 | 影子图书馆（非 OA） | 书目 + PDF；匿名每日有下载配额 |

反爬说明：被 403 不等于你的 IP 被封。`europepmc.org` 的网页在 Cloudflare challenge 后面，所以走 EBI 的 REST；
DOAB 的 `/rest` 拒绝无 JS 客户端，所以走它的 Atom feed；DOAB 详情页、Sci-Hub、Z-Library 用可解的工作量证明，
litcli 在本地做题后复用 cookie，不改写、不绕过。知网与豆瓣的滑块交你在可见浏览器手动完成。

## 配置

`~/.config/litcli/config.toml`（或 `$LITCLI_CONFIG`）；没有配置时用内置默认（Zotero + arXiv + Crossref + OpenAlex）。
完整字段见 [`config.example.toml`](config.example.toml)。

```toml
[backends.zotero]
type = "zotero_local"
base = "http://localhost:23119/api"     # 写库 key 由 `litcli backends authorize zotero` 自动写入

[network]
# proxy = "http://127.0.0.1:7890"       # 校园 / VPN；默认读 HTTP_PROXY / HTTPS_PROXY
```

## 已知限制

- 写库未授权时报 `HTTP 401`（退出码 2），不会污染库；先跑 `backends authorize zotero`。
- 专利检索要有 USPTO 免费 key：免 key 的公开源里专利记录趋近于 0。
- Europe PMC 的 OA 全文是**文本（JATS XML）不是排版 PDF**；Unpaywall 只给落地页时会退到链末，输出 `HTML` 提示浏览器打开。
- Sci-Hub 只按 DOI 解析，不能关键词检索；Z-Library 匿名下载有每日配额，可在配置里填 `email`/`password` 登录放宽。
- 配置里的 `api_key` 与 zlibrary 凭据是明文：`backends authorize` 会把配置 chmod 0600，手填凭据不会。
- arXiv 在受限沙箱里可能返回 406；`--all` 会跳过失败的后端继续。

## 架构

```
litcli/          cli.py（入口：解析/分发/异常映射）+ commands/（按意图分族的命令）+ backends/（可插拔来源）
                 models.py 唯一归一化模型 · aggregate.py 去重合并 · fulltext.py 全文链 · transport.py httpx 原语
tests/           unittest，全离线（stub 后端 / MockTransport）
skills/litcli/   agent 技能源，install.sh 按目标 agent 分发
```

分层：领域层（纯函数优先）→ 传输层 → 后端层（读写能力由 `capabilities` 标注）→ 配置层 → CLI 编排层。
各层职责与后端实现细节见 [`AGENTS.md`](AGENTS.md)，新加后端也照它来。

## 开发与发布

```bash
python3 -m unittest discover -s tests   # 全量离线测试
ruff check litcli tests                 # 规则集固定在 pyproject.toml
scripts/release.sh --check              # 发布门禁（另有 --dry-run / --bump / --push，详见 AGENTS.md）
```

CI 跑在 GitHub 镜像仓（`DarkOfTheMoon/litcli`，`master` 与 `v*` 触发 lint / 测试 / 打包）；Gitee 是发布主仓，
发版后由 `scripts/verify_release.sh vX.Y.Z` 按消费者视角复验产物。变更记录见 [`CHANGELOG.md`](CHANGELOG.md)。
