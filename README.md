# litcli — 学术文献的检索、引文与全文 CLI

[简体中文](README.md) · [English](README.en.md)

一条文献在多个来源之间用同一个模型读写：本机 Zotero 库、arXiv、Crossref、OpenAlex、知网题录、豆瓣图书，
以及 DOAJ / Europe PMC / DOAB / Zenodo 等开放获取语料。方向是给**你和你的 agent** 用——检索、读全文、
导出可喂 `pandoc --citeproc` 的引文、写回你自己的库。

- **不需要 Better BibTeX**：`export --format csljson` 直接产生 CSL JSON，`pandoc --citeproc` 能读。
- **不需要 zotero.org 账号**：走 Zotero 10 的本地 API（`localhost:23119`），写库一次授权即可。
- **没有 Zotero 也能用**：`--backend crossref` 等公网源单独就能检索、取全文、导引文。
- **不绕过验证码**：需要人机验证的站点（知网滑块、Sci-Hub、Z-Library、DOAB）由本地 `hashlib` 做题或交你手点，
  命中公开副本时输出会明确标注“公开副本（非 OA 授权）”。

## 60 秒上手（不需要 Zotero）

```bash
python3 -m venv .venv && . .venv/bin/activate              # 或用你已有的环境
pip install .                                             # Debian/Ubuntu 的 PEP 668 环境改用 ./install.sh
litcli search "graph neural network" --limit 5       # 默认搜全部来源；第一列 `来源/key` 可直接喂下一条
litcli data get 10.1038/s41598-024-80509-9 --backend crossref --format json
litcli export 10.1038/s41598-024-80509-9 --backend crossref --format csljson
litcli fetch 10.1038/s41598-024-80509-9            # 落到当前目录，stdout 只回路径
```

`search` 默认搜全部来源，`--backend` 收窄到单个；`--format json` 是一行一条记录，空结果打印 `[]`。
表格在 key 后标注来源性质：`[oa]` 开放获取授权、`[entitled]` 机构订阅（你的访问权限、非 OA 授权）、
`[public]` 公开副本（非 OA 授权）、`[local]` 你自己的库；JSON 里是对应的 `access` 字段。结果里出现 `[public]` 时还会在 stderr 打一行说明：那是 Sci-Hub / Z-Library 这类镜像给的副本，
**不是开放获取授权**，能不能再用得你自己看许可——这条责任不该藏在标签里。
`fetch` 默认把文件写到当前目录（`--out DIR` 另指），成功时 stdout 只有路径，失败原因走 stderr；
退出码 `0` 拿到文件 / `1` 没有全文 / `2` 出错。

## 安装

**推荐：装二进制，不需要 Python，也不需要下载源码。** 发布页提供安装器（附 `SHA256SUMS`）：

```bash
# Linux：自解压安装器，装到 ~/.local/opt/litcli-<版本>，链接到 ~/.local/bin/litcli
sh litcli-<版本>-linux-x86_64.run            # 加 --with-browser 顺带下载知网/豆瓣用的 Chromium
sh litcli-<版本>-linux-x86_64.run --uninstall
```

Windows 下载 `litcli-<版本>-windows-x86_64-setup.exe` 双击安装（按用户装到
`%LOCALAPPDATA%\Programs\litcli`，无需管理员）；安装向导里的"下载 Chromium"是勾选项。

安装器里只有冻结后的字节码与依赖，**没有 `.py` 源码**，目标机不需要装 Python。Chromium 不随包
（单引擎约 150 MB），用到知网 / 豆瓣时再装：

```bash
litcli backends install-browser        # 安装后再跑，或在安装器里勾选
litcli backends install-browser --check  # 自检：无头启动一次并报版本
```

浏览器落在 Playwright 自己的缓存（`~/.cache/ms-playwright` / `%LOCALAPPDATA%\ms-playwright`），
升级装新版会继续复用，卸载也不删。

**从源码装**（要改代码、或要跑测试时）：

```bash
pip install .                        # 装进当前环境（venv / 任意 Python 3.11+）
./install.sh                         # Debian/Ubuntu 的 PEP 668 环境：--user 快照到 ~/.local，不动系统包
export PATH="$HOME/.local/bin:$PATH"  # 若 ~/.local/bin 还不在 PATH（写进 shell 启动文件可持久）
litcli backends                      # 自测：列出已配置后端及能力
```

`install.sh` 装的是**源码副本快照**：生产期间改仓库不波及已装版本，发新版本重跑即可。开发时在仓库内直接用
`./bin/litcli`，与已装版本互不影响。PDF 文本抽取依赖系统 `pdftotext`（poppler-utils）；缺失时返回空文本，
其余功能不受影响。CNKI / 豆瓣另需 `./install.sh --with-cnki`（或 `--with-douban`）装浏览器依赖。

升级就是重跑 `./install.sh`（覆盖快照）；卸载用 `pip uninstall --break-system-packages litcli`
（PEP 668 机器上卸载同样要这个开关；venv 里不用）并删 `~/.config/litcli` 与 `~/.local/share/litcli`。

**Windows（源码）**：`install.sh` 与补全脚本是 POSIX 的，改用 `py -m venv .venv` 后 `.\\.venv\\Scripts\\Activate.ps1`
与 `pip install .`；仓库内调试用 `bin\\litcli.cmd`。PDF 文本预览需要把 poppler 的 `pdftotext.exe` 放进 PATH；
shell 补全只有 bash/zsh/fish（PowerShell 下可在 Git Bash 里生成）。配置在 `%APPDATA%\\litcli\\config.toml`。

数据都留在本机：引文写进你自己的 Zotero 库，配置在 `~/.config/litcli/config.toml`，其余只有对外 API 请求。
`install.sh` 还会把 `skills/litcli/SKILL.md` 分发给探测到的 agent（Claude Code / Codex / Cursor / Gemini，
格式各随其主流），并总留一份中立副本在 `~/.local/share/litcli/skill/SKILL.md`；`--no-skills` 跳过技能、
`--skills-only` 只装技能、`--skills claude,cursor` 只发给指定 agent。

## 命令

`--backend` 收窄到单个来源（不写就是搜全部已配置来源 + 免 key 的 OA 语料）；key 也可以自带来源前缀
（`openalex/W123`、`zotero/AB12`），有前缀时按前缀路由、`--backend` 仍然优先。`--all` 与 `--backend` 互斥。
聚合时会去重合并（同一作品跨来源并成一条，DOI/ISBN 优先），并在 `extra["sources"]` 标注来源。
表格第一列就是 `来源/key`，两种写法都认。
退出码：`0` 成功（含无结果）；`2` 出错或后端不支持；`fetch` 另用 `1` 表示「没有全文」。

检索是向外扇出：**单个来源失败默认不提**，加 `--verbose`（或 `LITCLI_VERBOSE=1`）才逐条说明哪个来源、
什么原因；全部来源都失败时即使不加开关也会明说并以 `2` 退出——那不算「没有结果」。
其余需要拿到确定结果的命令（`fetch`、`data`、`export`、`refs`、`citing`、`related`、`backends`）失败一律明说。
本机 Zotero 没开着时，读写会直接报连接失败（不用等超时）；`backends status` 会先探一次端口并标 `[unreachable]`，省得你按了 y 才发现。

元数据读取会缓存一天（`~/.cache/litcli/cache`，只存成功的 GET、不存文件、写操作不缓存），重复检索不必再花站点的配额；
要拿最新结果用 `--no-cache`。检索对来源是并发的（最多 4 路），整轮 45 秒上限：卡住的来源按超时跳过，不再把命令拖住。
`search` / `fetch` / `export` / `refs` / `citing` / `related` 只读，`data` 写库。

**检索与引文图谱**

| 命令 | 说明 |
|---|---|
| `litcli search "X" [--all] [--backend B] [--type T] [--limit N] [--no-cite]` | 检索候选（`--format table\|json\|csl`）；`--no-cite` 不在表格里附行内引文；字段过滤与排序见下节 |
| `litcli refs <key>` / `citing <key>` / `related <key>`（均可 `--limit N`、`--sort cited`、`--format table\|json\|csl`） | 参考文献（回溯奠基）／引用该文的文献（前向跟进）／语义邻居（OpenAlex 图谱；该文没有邻居列表时退回按标题实词检索并标注） |
| `litcli data get <key> [--format table\|json\|csl]` | 单条全字段 |
| `litcli export <key>... [--format csljson\|bibtex\|ris\|text] [--no-cite]` | 导出引文；`csljson` 可喂 `pandoc --citeproc`；`--no-cite` 只对 `text` 生效。arXiv key 仅在 `--backend openalex\|crossref` 下归一为 DOI |
| `litcli fetch <DOI 或出版社文章 URL> [--out DIR] [--no-download] [--lines N] [--email ADDR] [--scihub]`（旧动词 `fetch fulltext <doi>`、旧旗标 `--download` 仍接受） | 保存全文：本地库 → Unpaywall → OpenAlex → Europe PMC → Zenodo → 各出版社订阅副本（按其 DOI 前缀自动尝试，见下表）→ 落地页；`--lines` 顺带预览正文前 N 行，`--email` 作为 Unpaywall/OpenAlex 礼貌池的联系邮箱 |
| `litcli fetch --book "书名\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--limit N] [--pick N]` | 图书候选 → 选定补全书目 → 保存文件（ISBN 自动走此路径）；`--limit` 候选条数、`--pick` 选第几条 |

**写库**（默认 Zotero；只读后端会报 `[skipped]` 并以退出码 2 结束）

| 命令 | 说明 |
|---|---|
| `litcli backends authorize zotero` | 一次性授权写库 key（本地弹框批准后写回配置） |
| `litcli data add --title "..." --creator "家族, 名字" [--year ..] [--doi ..] [--type ..] [--url ..] [--abstract ..] [--force]` | 摄入元信息；写前过查重 / 半残门，`--force` 跳过 |
| `litcli data add --pdf <file> [--yes]` / `--csl <file>` | 从 PDF 首页启发式抽候选（交互确认 y/N 后写库，`--yes` 跳过）或从 CSL JSON 摄入 |
| `litcli data set <key> --title ..`（另有 `--abstract/--doi/--url/--year`） · `data del <key>` · `data attach <key> <路径\|URL>` | 改 / 删 / 挂全文 |

**诊断**

| 命令 | 说明 |
|---|---|
| `litcli backends list` · `backends status` | 已配置后端与能力 / 授权与可达状态 |
| `litcli completions <bash\|zsh\|fish>` | 打印自包含补全脚本；`eval "$(...)"` 后按 TAB 补全 |
| `litcli backends install-browser [--check] [--dry-run] [--with-deps]` | 下载浏览器；`--check` 无头启动验证、`--dry-run` 只显示下载源、`--with-deps` 顺带 apt 装系统库 |
| `litcli data members <集合key> [--no-recursive] [--limit N] [--format table\|json]` | 罗列集合条目（默认递归子集合，`--no-recursive` 只看本层；`--format json` 一行一条记录） |
| `litcli --version` | 打印版本并退出 |
| `litcli --no-cache <命令>` | 跳过响应缓存（`LITCLI_NO_CACHE=1` 同样生效）；缓存默认 24 小时、只存成功的元数据读取 |
| `litcli --verbose <命令>` | 检索里失败来源的逐条说明（默认静默；`LITCLI_VERBOSE=1` 同样生效），只写 stderr |
| `litcli --debug <命令>` | 把请求、状态、耗时、限速等待与重试逐行写到 stderr（`LITCLI_DEBUG=1` 同样生效）；意外异常额外打印调用栈。stdout 不受影响 |

`data tree` / `data members <集合key>` 罗列集合；`--year` 等过滤在各后端的映射见下节。

### 课题调查（survey）

把「围绕一个课题收文献」做成长跑项目：`survey new` 建项目（种子、年份窗口、刊名约束、阈值、档位落 `project.toml`），`survey run` 沿引文图谱一跳一跳收网，收到的全文与元数据落 `corpus/`，每一步落 `trace.jsonl` 供续跑与事后审查。

| 命令 | 说明 |
|---|---|
| `litcli survey new "<课题>" [--seed DOI]... [--year A-B] [--type T] [--container 刊名] [--threshold N] [--effort 档]` | 建项目，只打印项目名；不给 `--seed` 先检索取前 3 条当种子 |
| `litcli survey run <项目> [--effort 档] [--format jsonl\|md]` | 从最后一个检查点继续；stdout 是 JSONL 事件，`--format md` 换成按跳的表格 |
| `litcli survey status <项目>` | 一行 JSON：课题、状态、跳数、准入数、最后停因、剩余额度 |
| `litcli survey stop <项目>` | 写 `$.stop`，循环在下一个跳边界收尾 |

档位决定走多宽，不决定收多少条——热的题每跳都有新高分就一直走到额度或饱和，冷的题几跳没动静就自己停：

| 档 | 前沿/跳 | 准入/跳 | 邻居/条 | 连续无新高即停 | openalex 上限 | arxiv 上限 | 每出版社 | 最长跳 |
|---|---|---|---|---|---|---|---|---|
| low | 4 | 2 | refs 10 / citing 10 / related 5 | 2 | 40 | 10 | 2 | 6 |
| medium | 8 | 5 | 20 / 20 / 10 | 3 | 200 | 40 | 6 | 20 |
| high | 16 | 8 | 30 / 30 / 15 | 4 | 600 | 100 | 15 | 50 |
| max | 24 | 12 | 50 / 50 / 25 | 6 | 1500 | 200 | 30 | 120 |

额度按请求计：命中缓存不扣、重试照扣。同一项目同时只允许一个 `run`（存在 `$.lock` 即退 2）；续跑用同一条命令，已展开过的条目不再展开。`seen.jsonl` 每条都带理由（`score 3.21 >= 2.50`、`seen`、`year … outside …`），下载失败只标 `unavailable`、不剔除候选。只有收尾时 `corpus/` 为空才退 1。

### 过滤条件在各后端的映射

后端没有对应字段时，尽量折进自由查询词，否则忽略。

- **crossref** → `query.title` / `query.author` / `query.container-title` / `filter=from-pub-date,until-pub-date`
- **openalex** → `filter=title.search:` / `raw_author_name.search:` / `publication_year:`；`container` 折进自由查询
- **arxiv** → `ti:` / `au:` / `submittedDate:[…]`，用 ` AND ` 拼 `search_query`
- **zotero** → 全部折进单个 `q`（`qmode=everything` 全字段命中）

`cited_by_count` 取 openalex / crossref 的对应字段，table 输出显示 `[cited N via 来源]`（来源之间数字不一致时标注 `(sources disagree)`）。
表格在标题下给一行摘要片段（160 字），判断要不要更省事。默认按相关度排序（标题命中 > 摘要命中，命中全部词再加分，同分看被引与年份），`--sort cited` 改按被引降序；
字段过滤除了映射给后端，还会在本地再判一次，后端忽略条件时记录不会漏出。

## 后端与授权边界

默认启用 Zotero、arXiv、Crossref、OpenAlex。其余随时可用单次 `--backend` 直接指定（不必写配置）。
免 key 的 OA 语料（`doaj`/`europepmc`/`doab`/`zenodo`）**无需配置就参与** `--all` 聚合与 `fetch` 默认链；
只有想在配置里调整它们、或让其它后端（如 `uspto`、`cnki`、`douban`）进入聚合，才需要写 `config.toml`。
CNKI / 豆瓣另需浏览器依赖。

| 后端 | 能力 | 授权 | 取什么 |
|---|---|---|---|
| `zotero` | 读 / 写 / 附件 | 你的本机库 | 本地 API，写库一次授权 |
| `arxiv` · `crossref` · `openalex` | 读（arxiv 另给 PDF 直链） | 开放，免 key | 元数据；arxiv / openalex 按 IP 限速并已退避 |
| `doaj` · `europepmc` · `doab` · `zenodo` | 读 | 开放获取（OA），免 key | 公开 OA 记录；europepmc 给 JATS XML、zenodo 给开放文件 |
| `openlibrary` · `uspto` | 读 | 开放（uspto 需免费 key） | 图书题录 / 专利全文；默认不内置 |
| `cnki` | 读 | 知网网页 | 公开**题录**，不抓付费墙全文；需可见浏览器、人工过滑块 |
| `douban` | 读 | 豆瓣网页 | 公开**书目**（书名/作者/ISBN/版次/页数），默认无头 |
| `sciencedirect` | 读 | 订阅内容，凭你的机构授权（不是 OA） | Elsevier DOI 的全文 PDF；只对 `10.1016` 等 Elsevier 前缀自动尝试 |
| `wiley` | 读 | 订阅内容，凭你的机构授权（不是 OA） | Wiley DOI 的全文 PDF；只对 `10.1002` 等 Wiley 前缀自动尝试 |
| `springer` | 读 | 订阅内容，凭你的机构授权（不是 OA）；金 OA 文章免配置 | Springer Nature DOI 的全文 PDF；只对 `10.1007` 等前缀自动尝试 |
| `cambridge` | 读 | 订阅内容，凭你的机构授权（不是 OA）；CC 许可的 OA 文章免配置 | Cambridge DOI 的全文 PDF；只对 `10.1017` 前缀自动尝试 |
| `aip` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 Cloudflare 挑战后 | AIP DOI 的全文 PDF；只对 `10.1063`、`10.1121` 前缀自动尝试 |
| `acm` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 Cloudflare 挑战后 | ACM DOI 的全文 PDF；只对 `10.1145` 前缀自动尝试 |
| `aps` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 Cloudflare 挑战后 | APS DOI 的全文 PDF；只对 `10.1103` 前缀自动尝试 |
| `iop` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 Cloudflare 挑战后 | IOPscience DOI 的全文 PDF；只对 `10.1088` 前缀自动尝试 |
| `tandf` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 Cloudflare 挑战后 | Taylor & Francis DOI 的全文 PDF；只对 `10.1080` 前缀自动尝试 |
| `aiaa` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 Cloudflare 挑战后 | AIAA DOI 的全文 PDF；只对 `10.2514` 前缀自动尝试 |
| `ieee` | 读 | 订阅内容，凭你的机构授权（不是 OA）；站点在 WAF 挑战后 | IEEE Xplore 文章的全文 PDF；只对 `10.1109` 前缀自动尝试 |
| `scihub` | 读 | 公开副本（非 OA 授权），只按 DOI | PDF；加 `--scihub` 才兜底（复制 `config.example.toml` 也会启用，因为示例里留着 `[backends.scihub]` 段） |
| `zlibrary` | 读 | 公开副本（非 OA 授权） | 书目 + PDF；匿名每日有下载配额 |

反爬说明：被 403 不等于你的 IP 被封。`www.sciencedirect.com` 对无 JS 客户端先给 Cloudflare 挑战、再按会话判权限（`CPE00001`），
所以它不像其它源那样"免配置即通"：在用**已认证**（机构登录 / 校园网）的浏览器标签里打开一次文章，把会话 Cookie 连同该浏览器的 UA 配进
`[backends.sciencedirect]`（`cookie` / `cookie_file` + `user_agent`，二者缺一不可——清关 Cookie 同时绑定 **UA 与出口 IP**，
寿命分钟到小时级、服务端随时可能吊销，换机器回放会直接失效）；该区甚至不认无头浏览器——同一份 Cookie、同一个 UA 放进无头 Chromium
仍是 `CPE00001` 或挑战页（2026-11-24 实测），所以它只认你在**可见**浏览器里拿到的那份会话；未认证的标签页只会得到 "Purchase PDF"，且 `/pii/` 会被静默改写成 `/abs/`；
`onlinelibrary.wiley.com` 是同一套形态、同一套配置法（`[backends.wiley]`；它的清关 Cookie 虽可存活一年，仍随会话吊销，未订阅时 PDF 端点会静默跳到摘要页）；
Wiley 另有 **TDM token**（`[backends.wiley].tdm_token`，免费申请）：走 `api.wiley.com`，不经过 Cloudflare，只放行机构已订阅的文章；
`link.springer.com`（`[backends.springer]`）**没有 Cloudflare**、是另一种形态：文章页匿名就能读，题录与 PDF 链接恒在，金 OA 的 PDF 匿名也能下载，
只有付费文章的 PDF 需要会话——该端点先 303 到 `idp.springer.com` 换身份 cookie，未订阅的会话最后落回文章页（静默降级，不是 403），
同样需要**已认证**会话的 cookie + `user_agent` 成对配置（会话绑 UA，过期就重取；OpenAlex 给出的题录仍可正常读出）；
`cambridge.org`（`[backends.cambridge]`）也与 Cloudflare 无关：条目页匿名可读、题录恒在，CC 许可的 OA PDF 匿名直取；
付费条目经 `doi.org` 只落到 `/abs/` 短页，其 PDF 端点把匿名客户端 302 回条目页（静默降级），未获授权时 `fetch` 报因并给链接；
`pubs.aip.org`（`[backends.aip]`）相反，**在 Cloudflare 托管挑战后面**：文章页与 `article-pdf` 端点对无 JS 客户端一律 403 + `cf-mitigated: challenge`，
因此 AIP 基本必须配 `[backends.aip]` 的 `cookie` + `user_agent`（`cf_clearance` 绑 UA 与出口 IP，过期就重取），未配时报的是这道门而不是 IP 被封；
ACM（`dl.acm.org`）、APS（`link.aps.org`）、IOPscience（`iopscience.iop.org`）、Taylor & Francis
（`tandfonline.com`）、AIAA（`arc.aiaa.org`）与 AIP 同一形态：都在 Cloudflare 托管挑战后，页面与 PDF 端点对裸客户端一律 403，
必须成对配置 `cookie` + `user_agent`；IEEE Xplore（`ieeexplore.ieee.org`，`[backends.ieee]`）换了一种闸——WAF 的 JS 挑战
（AWS WAF 的空 202 包体 + `gokuProps`，靠 `aws-waf-token` 清关；2026-11 起又叠了一层 F5 的
`<APM_DO_NOT_TOUCH>` 拦截页，带 200 与文章标题、只靠 `/TSPD/…` 脚本放行，靠 `TS*` cookie）——未授权会话的 PDF 端点
会 302 到 `/Xplore/login.jsp?…&authDecision=…`；这六家同样只取机构已订阅或 OA 的正文，单 DOI、低频。
`europepmc.org` 的网页在 Cloudflare challenge 后面，所以走 EBI 的 REST；
DOAB 的 `/rest` 拒绝无 JS 客户端，所以走它的 Atom feed；DOAB 详情页、Sci-Hub、Z-Library 用可解的工作量证明，
litcli 在本地做题后复用 cookie，不改写、不绕过。知网与豆瓣的滑块交你在可见浏览器手动完成。

**不想搬 cookie 就走浏览器会话**：装好 Chrome，`litcli browser login sciencedirect`（站点名见 `litcli browser status`）
在弹出的窗口里机构登录一次，浏览器保持运行；之后 `fetch` 对这些出版社的取全文就走这只活着的浏览器，
页面与 PDF 都在页内取，凭据不离开浏览器、过期也会自己续。`litcli browser status` 逐站看 `signed in` / `signed out` / `gated`。
它由 `[browser].enabled = true` 打开（默认关闭）；关着时逐字走上面的 cookie 路径，配了 cookie 照样能工作。
浏览器沿用 `[network].proxy`——清关绑出口 IP，两边必须是同一出口。

## 配置

`~/.config/litcli/config.toml`（Windows 为 `%APPDATA%\litcli\config.toml`；`$LITCLI_CONFIG` 可另指）；
没有配置时用内置默认（Zotero + arXiv + Crossref + OpenAlex）。
文件里列出的后端默认全部启用；想留着字段又临时关掉某一段，在该段写 `enabled = false`（连免 key 的 OA 语料也适用）。
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
- ScienceDirect 的 PDF 另有**第二道验收关**（`craft/challenge/pdf`），页面能开也未必能直取：`fetch` 走后端会话下载，被拦时明确报因并给出 PDF 链接，让你在已授权的浏览器里打开。Wiley 的 PDF 端点对未订阅会话返回的是跳转后的 HTML 摘要页而非 403，`fetch` 会明说"未获授权或会话已失效"并给链接。SpringerLink 同理：未订阅（且非金 OA）时会话会被送回文章页，`fetch` 报"未获授权或会话已失效"并给链接。三者下载失败都会先把链的尾部（合法落地页、`--scihub`）走完再报错。
- 二进制安装器不含 Chromium：知网 / 豆瓣要先 `litcli backends install-browser`，否则报错并给出该命令。
- 配置里的 `api_key`、zlibrary 凭据，以及出版社会话的 `cookie` / `cookie_file` 都是明文：`backends authorize`
  会把配置 chmod 0600，手填的不会；导出的 cookies.txt 也按普通文件保存，别提交进公开仓库。
  Windows 上 chmod 只等于只读位，真正的隔离来自 `%APPDATA%` 的用户目录 ACL。
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
python3 -m pip install -e '.[dev]'      # 维护工具：build + 固定版 ruff + pyinstaller（运行时不装）
python3 -m unittest discover -s tests   # 全量离线测试
ruff check litcli tests                 # 规则集固定在 pyproject.toml
scripts/release.sh --check              # 发布门禁（另有 --dry-run / --bump / --push，详见 AGENTS.md）
```

`release.sh` 先自检 `ruff` 与 `build` 是否齐备（缺了直接给上面那条安装命令）；`--binary` 发版还需要
[GitHub CLI](https://cli.github.com)（`gh`）去取另一平台的安装器。

CI 跑在 GitHub 镜像仓（`DarkOfTheMoon/litcli`，`master` 与 `v*` 触发 lint / 测试 / 打包）；Gitee 是发布主仓，
发版后由 `scripts/verify_release.sh vX.Y.Z` 按消费者视角复验产物。变更记录见 [`CHANGELOG.md`](CHANGELOG.md)。
