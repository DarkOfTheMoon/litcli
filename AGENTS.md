# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 运行与命令

**零第三方依赖**：仅用 Python 3.11+ 标准库（`tomllib`、`urllib`、`xml.etree`、`subprocess`）；本机是 3.12。生产走标准包安装（`pyproject.toml` 声明入口点 `litcli = "litcli.cli:main"`）；**本机 Python 3.12 受 PEP 668（externally-managed）限制**，`install.sh` 用 **`pip install --break-system-packages --user .`（非 editable）**：安装时把源码**复制**为冻结快照到 `~/.local`，生产 `litcli` 与仓库源码**解耦**（生产期间改源码不波及已装版本）；发布新版本重跑安装。零第三方依赖故 `--break-system-packages` 无冲突之虞，`--user` 限用户作用域。开发在仓库内走 `./bin/litcli`（独立用源码，互不影响）。PDF 文本抽取需系统装了 `pdftotext`（poppler-utils）。

```bash
# 生产方式(推荐):./install.sh = pip install --break-system-packages --user .(复制快照,无 venv)
./install.sh
litcli <子命令>

# 开发方式:仓库内直接跑(免安装,任意 cwd 可用;与生产快照隔离)
./bin/litcli <子命令>            # wrapper:把项目根注入 sys.path
python3 -m litcli.cli <子命令>  # 等价

# 跑全部测试（unittest，非 pytest）
python3 -m unittest discover -s tests

# 跑单个模块
python3 -m unittest tests.test_backends

# 跑单个用例
python3 -m unittest tests.test_models.TestModels.test_to_csl
```

CLI 帮助用 `./bin/litcli --help`；本机 Zotero 库用 `litcli search "..."`（`--all` 跨后端聚合，`litcli backends list/status` 看可用后端与授权状态；`search --type <article|book|...>` 按类型过滤降噪，`--title/--author/--year/--container` 字段级过滤、`--sort cited` 按被引降序；`litcli refs <key>`(该文的参考文献/回溯找奠基)、`litcli citing <key>`(引用该文的文献/前向找跟进)、`litcli related <key>`(语义相关文献)——三者均 OpenAlex 引文图谱、默认按被引降序；`related` 默认按语义相关度内联序、`--sort cited` 才按被引降序（`refs`/`citing` 天然按被引降序）。`export` 的 arXiv key 如 `arxiv/1806.09180` 会自动归一为 DOI `10.48550/arxiv.x`）。shell 补全用 `litcli completions <bash|zsh|fish>` 生成自包含脚本（`eval "$(litcli completions bash)"` 后 `litcli <TAB>` 补子命令/参数选项/选项取值(如 `--format`)/本地文件路径；补全时**不再起 `litcli` 子进程**——spec 内嵌在脚本里，CLI 面更新后重新 eval 即可）。

## 架构分层（后端无关的领域层 + 可插拔后端层）

litcli 的核心是「一条文献在多个后端（Zotero / arXiv）间的读到写」统一归一化。五层自上而下：

- **领域层**（与任何参考管理器无关，`litcli/models.py`、`citation.py`、`pdf.py`、`metadata.py`）：
  - `models.AcademicItem` 是唯一归一化模型（`source/key/title/creators/year/abstract/doi/isbn/edition/publisher/url/item_type/attachments/extra`），与 CSL JSON 双向转换（`to_csl` / `from_csl`）、可导 BibTeX。图书条目在 `doi` 之外新增两个一等字段：`isbn`（与 DOI 同级的图书身份键）与 `edition`（版次）；四类导出（csljson/bibtex/ris）均已感知——CSL 的 `"ISBN"`/`"edition"`、BibTeX 的 `isbn = {...}`/`edition = {...}`、RIS 的 `SN`/`ET` 字段。
  - `citation.py` 提供零依赖的 author-date in-text / 参考文献简版渲染；正统路径是 `export --format csljson` 喂 `pandoc --citeproc`，**不依赖 Better BibTeX**。
  - `pdf.py`：下载 + `pdftotext` 抽文本，后端无关的「拿到 bytes/路径 → 抽文本」。
  - `metadata.py`：纯启发式从 PDF 首页文本猜 title/authors/year/abstract，**不调 LLM、完全离线**；`from_text_lines` 是纯函数（可脱离 pdftotext 复用）。`data add --pdf` 用它抽候选、展示后经**交互确认（y/N）**才写库（`--yes` 跳过确认、`--force` 跳过去重门），不自动写。

- **聚合层**（`litcli/aggregate.py`，纯函数、不碰后端不联网）：供 `search --all` 用的去重/合并/排序。身份键**优先生效 DOI（期刊/论文）、其次 ISBN（图书）**、无二者退化为归一标题（`norm_title` / `norm_doi` / `norm_isbn`）；`norm_doi` 剥 `https://doi.org/` 前缀、`norm_isbn` 剥空格/连字符/点——Crossref 给裸 DOI、OpenAlex 给完整 URL，ISBN 有 `978-7-118-04834-8` 与 `9787118048348` 两种写法，不规范化会让同一本书身份键各异而无法去重；`_merge_two` 只把缺失字段（含 container_title/volume/issue/page/publisher/isbn/edition/cited_by_count 及 extra["openalex_id"]）补给最完整记录（副本操作，不污染入参，见 `test_does_not_mutate_input`），`_info_score` 亦计 `item.isbn`；`extra["sources"]` 记录全部来源；排序按年份降序、同年标题升序。注意：预印本与刊出本是两个不同 DOI/作品，语义上不应合并。

- **后端层**（`litcli/backends/`）：`base.py` 定义抽象 `Backend` + `capabilities`（`read`/`write`/`attachment`）。写操作 `add`/`patch`/`delete`/`attach` 内部先 `_require("write")`，只读后端抛 `NotSupportedError`（CLI 捕获后输出 `[skipped]` 并以退出码 2 返回）。
  - `zotero_local.py`：Zotero 10 本地 API（`localhost:23119/api`，`users/0` 本机别名，**无需 zotero.org Web API key**）。读+写+附件，写入用 `If-Unmodified-Since-Version` 并发控制，写请求需 `Zotero-Server-ID`（从 `/api` 响应头缓存，非 `/connector/ping` 的 body——后者是 HTML）。`search` 用 `qmode=everything`（默认 `titleCreatorYear` 漏摘要字段，中文/摘要词命中需此模式）；`ping()` 供 `backends status` 轻量探测本机库是否在运行。写库 key 经 `litcli backends authorize zotero` 调 `/api/local/authorize` 运行时授权并写回 `config.toml` 的 `[backends.zotero].api_key`；`delete` 走 DELETE，`attach` 挂 link 模式附件（本地→linked_file，URL→linked_url）。
  - `arxiv.py`：arXiv 开放 API（Atom/XML），只读 + 可拉 PDF 直链，按 arXiv id（去 `v` 版本）归一化。按 IP 限速（连发 `HTTP 429 Rate exceeded.`），`_query` 对 429/5xx 按 2s/4s 退避重试并给明确提示；UA 描述性、可经 `[backends.arxiv].mailto` 加联系邮箱（礼貌池）。
  - `crossref.py` / `openalex.py`：开放元数据（DOI / 学术图谱），只读，5xx 指数退避重试；OpenAlex 摘要为倒排索引需重建。`search(query, limit, filters)` 把 `--title/--author/--year/--container` 映射为各后端过滤词（crossref → `query.title/author/container-title` + `filter=from-pub-date`；openalex → `filter=title.search/raw_author_name.search/publication_year`，container 折叠进自由 `search`；arxiv → `ti:`/`au:`/`submittedDate` AND 语法；zotero → 折进单 `q`）。`_work_to_item`（openalex）填 `cited_by_count` + `extra["openalex_id"]`，且 `url` 是真实落地页（`https://doi.org/<doi>` → OA landing → openalex.org 页，不再存裸 DOI）；`_get` 走 `_RateLimiter`（默认 ~10 req/s）限速礼貌池，`mailto` 经 `_with_mailto` + UA 携带。引文图谱三方向为本后端独占能力（base 默认 `NotSupportedError`）：`related(key, limit)` 读父 work 内联 `related_works` 逐个取全量；`references(key, limit)` 读内联 `referenced_works` **并行拉取全部**、按被引降序后截断到 `limit`；`citing(key, limit)` 走 `filter=cites:<W-id>&sort=cited_by_count:desc` 单请求拿全量。`related`/`references` 共用 `_inline_works`（父 work 内联 W-id 列表 → `_fetch_works` 并行拉取全部、按被引降序后截断到 `limit`——按被引排序必须取全量，才能挑出出现在内联列表后段的高被引文献；`citing` 不走它）。
  - `openlibrary.py`：开放图书（`search.json`），只读；**默认内置不含**（部分网络无路由），在 config 显式启用。
  - `uspto.py`：USPTO 专利全文检索，只读 + **key 门控**（免费注册）；免 key 的 Crossref/OpenAlex 公开版专利记录趋近 0，专利以本后端为主通道。
  - `cnki.py`：知网中文题录检索，只读 + **Playwright 可选依赖**。与其它后端不同，网络层用 **Playwright 而非 urllib**——不继承 `retry_json`。`search` 懒加载 `playwright.sync_api`（缺失时抛明确提示装 `install.sh --with-cnki`），`chromium.launch_persistent_context` 复用用户**已登录**的独立 profile（`~/.config/litcli/cnki-chrome`，不碰主 Chrome）；知网对无头访问必弹滑块（探针实测 `/verify/home`），故 `headless` 默认 False，遇滑块等用户**在可见浏览器手动完成**，**不程序化绕过**。合规：只从结果列表页取**公开题录**（title/creators/container/year/abstract），**不实现任何抓全文路径**。解析拆两层：纯函数 `rows_to_items`（输入虚拟行 dict，离线可测、知网改版只改适配层）克 `_virtualize_rows`（薄 Playwright 适配），`_normalize_author` 处理中文作者（整串 CJK 作 family，`given` 空）。
  - `scihub.py`：Sci-Hub 全文兜底，只读、**只按 DOI 解析**（2026 起站点首页只收 DOI/URL，旧的 `/search?query=` 返空，故 `search` 仅对 DOI 形态查询出单条，关键词返回 `[]`）。**排在合法全文链最后、默认不介入**：`fetch fulltext --scihub` 或配置 `[backends.scihub]` 才启用，命中标 `kind="scihub"`。与其它 HTTP 后端不同，它要过 DDoS-Guard cookie 墙 + **ALTCHA 工作量证明**：`solve_altcha` 用本地 `hashlib` 解页面自己出的 SHA-256/SHA-384/SHA-512 题（不改写、不绕过），`_Http` 用 cookie jar 维持会话串完 challenge→solution→reload。`capabilities` 刻意只标 `read`（不标 `attachment`），以免被 `_from_local` 当成本地附件而抢在 Unpaywall 之前。解析拆纯函数（`extract_pdf_url` / `parse_citation_meta` / `solve_altcha` / `captcha_id`）克薄适配。合规：盗版镜像，输出明示“非 OA 授权”；单 DOI、低频。
  - `douban.py`：豆瓣图书搜索，只读 + **Playwright 可选依赖**，`item_type="book"`。与知网同构：无公开 API（`api.douban.com/v2/book/search` 缺 key 恒 400）→ 用 Playwright 渲染。**反爬弱于知网**：搜索页 SSR 直接注入 `window.__DATA__`（顶层 `count/error_info/items`，`items` 每条含 title/url/id/rating/abstract，`abstract` 是"作者 / 出版社 / 出版年 / 定价"一条），故 `headless` 默认 **True**（无需人工过滑块）；仅被风控（`检测到异常流量`）才在可见浏览器人工处理，**不程序化绕过**。`search()` 读 `window.__DATA__` 出**候选**书单（title/author/publisher/year/rating，`get` 才补全）；`get(key)` 打开 `book.douban.com/subject/<id>/` 渲染，读 `#info`（`作者: …\n出版社: …\n出版年: …\nISBN: …\n页数: …\n定价: …`，**块级元素故 `innerText` 用 `\n` 分隔，勿用 `|`**）→ `_parse_info` 拆字段 → 完整书目含 ISBN/出版社/版次/出版年/页数。解析亦拆两层：纯函数 `window_data_to_items` / `_parse_info` / `_subject_to_item`（离线可测、豆瓣改版只改适配层）克 Playwright 适配（`_read_payload` / `_wait_for_info`）；`_subject_url` 把 `<id>` / `subject/<id>` / 整 URL 归一化。合规：只取**公开书目元数据**（书名/作者/出版社/ISBN/版次/出版年/页数/评分），不抓受限内容、低频检索。
  - `zlibrary.py`：Z-Library 图书检索 + 下载，只读、影子图书馆（非 OA 授权）、`item_type="book"`。零依赖 `urllib`（页面 SSR，无需 Playwright），用前过站点 **“Checking your browser” 工作量证明**——混淆串解出常量后，标记位在摘要第 `int(常量[0],16)` 字节，`sha1(常量+n)` 满足 `[i]==0xB0 && [i+1]==0x0B` 即通过，成功写 `c_token`/`c_time` cookie 后重载（本地 `hashlib` 做题，不改写、不绕过）；`_Http` 自 `scihub` 复用（cookie jar + 节律 `delay`），解一次即重试。**入口域名会轮换**（`z-library.by` 302 到真实前端主机且丢路径），故只配入口，`_resolve_base` 从重定向发现主机、镜像按序尝试，路径再拼到发现的主机上。`search` 引号包查询出 `<z-bookcard>` 候选（title/author/publisher/year/rating，`year` 映射 `yearFrom/yearTo`，title/author 后置过滤）；`get` 读 `bookProperty property_*` 块 + `h1.book-title`/`i.authors`（JSON-LD 仅兜底），ISBN-13 优先、`pages` 为 "0" 记空；`get_attachment` 取 `/dl/<hash>` 并校验 `%PDF` 魔数。**匿名每日下载有配额**（204 或 `限额已用完` → 明确报错）；可选 `email`/`password` 经 `POST /login`（`site_mode=books`）登录放宽，失败只提示一次、不反复重试。`capabilities` 只标 `read`（不标 `attachment`，与 scihub 同因）。解析拆纯函数（`challenge_token` / `book_path` / `parse_cards` / `parse_book_page`）克薄适配，key 归一为相对 `book/<hash>`。
  - `doaj.py`：DOAJ 开放获取期刊索引（`doaj.org/api/search/articles`），只读、免 key，全部记录天然 OA。`search` 把字段过滤映射为 ES 语法（`bibjson.title/author.name/journal.title/year`），`get` 按 DOI 回查；记录以 DOI 为身份键，命中后交给合法 OA 全文链。解析纯函数 `record_to_item`（API 外层 `{bibjson: …}` 与裸 `bibjson` 都接）。
  - `europepmc.py`：Europe PMC（EBI REST）生物医学题录，只读、免 key。`search` 用 `TITLE/AUTH/JOURNAL/PUB_YEAR` 字段；`get` 接 DOI 或 `PMC…`/`PMID`。开放获取记录在 `attachments` 给出 **JATS XML**（`…/fullTextXML`），供 `fetch fulltext` 当合法全文；`capabilities` 只标 `read`（XML 是 URL 不是本地副本，同 scihub 之因）。lite 结果的 `authorString` 是“姓 首字母”格式，`_creators` 把末位首字母认作 given（多词姓氏靠该首字母保留完整；`Jr` 等后缀归入 family；群体作者独立成条不并入上一位作者）。europepmc.org 网页在 Cloudflare managed challenge（Turnstile）后面——urllib、假浏览器头、无头/有头 Chromium 实测均过不去——故链接按标识符拼，不取页面；非 OA 的 in-EPMC 记录 `fullTextXML` 返回 500，正文只在页面里。
  - `doab.py`：DOAB 开放获取图书，只读、免 key。该实例用 **Anubis** 防爬，两道墙要分开处理：`/rest/*` 对无 JS 客户端一律 `403 You address is not allowed to access this API.`（故检索改走免墙的 OpenSearch **Atom feed**，key 用裸 handle）；`/handle/*` 是**可解的**工作量证明——`sha256(randomData + n)` 前缀 `difficulty` 个十六进制零，解开后 GET `api/pass-challenge?id&response&nonce&redir` 换 auth cookie，之后正常取页（本地 `hashlib` 做题，不改写、不绕过；cookie 经 jar 复用，只解一次）。页面详情解析为 `<h5>标签</h5>值` 块（ISBN/出版社/页数/DOI/Download Url(s)/Rights…），标题取 `page-header` 的 `h1/h2`、退化到 `<title>`。纯函数 `solve_challenge` / `page_to_item` / `feed_to_items` / `entry_to_item`（`link.get("href")`——Element 的 `[]` 是子节点索引）。feed 里混入的**出版社/imprint handle**（标题为 "Untitled"、无日期/摘要）由 `is_imprint_placeholder` 在检索时丢弃，`get` 见 `DC.type=publisher` 则报“不是图书”。作者取全部 `DC.creator`/`DC.contributor` meta（`<h5>Author(s)</h5>` 块每人一个 `<div>`，块正则只看得见第一个）；`get_attachment` 依次尝试全部 Download Url、只认 `%PDF` 魔数（部分记录首个链接是 DOI/出版社落地页），全不为 PDF 才明确报错。`--year` 后置过滤。
  - `zenodo.py`：Zenodo（CERN 开放仓库）记录检索 + 文件解析，只读、免 key，`resource_type.type` 映射 `item_type`（dataset/software/thesis/…）。`get` 接数字 id 或 `10.5281/zenodo.<id>`；PDF 文件（按 `key` 后缀判断——`mimetype` 在搜索结果里常缺、内容 URL 以 `/content` 结尾）进 `attachments`，供 `fetch fulltext` 取开放文件。解析纯函数 `record_to_item`。

- **配置层**（`litcli/config.py`）：读 `~/.config/litcli/config.toml`（不支持则在 `$LITCLI_CONFIG`），`tomllib` 解析。加载优先级：显式 path → `$LITCLI_CONFIG` → `~/.config/litcli/config.toml` → **内置默认**（Zotero + arXiv + crossref + openalex）。写 Zotero 经 `litcli backends authorize zotero` 触发 `/api/local/authorize` 运行时授权并把本地 key 写回 `[backends.zotero].api_key`（`config.write_backend_key`，手写文本 upsert 保其它段）；检索专利（uspto）需在 config 配真实 `api_key`。后端注册表用 try-import：某后端文件未就绪时不影响其它后端工作。配置示例见 `config.example.toml`。

- **CLI 编排层**（`litcli/cli.py`）：argparse 意图分族子命令：`search`（检索，`--type` 按类型过滤、`--title/--author/--year/--container` 字段级过滤、`--sort cited` 按被引降序）、`refs`/`citing`/`related`（引文图谱三方向——参考文献回溯/引用前向/语义邻居，默认 openalex，不支持的只读后端 `[skipped]` 退出码 2）、`data get/add/set/del/attach/tree/members`（本地库增删改查 + 集合罗列）、`fetch fulltext`（全文定位：本地库 → Unpaywall/OpenAlex → Europe PMC XML/Zenodo 文件 → Unpaywall 落地页；`--scihub` 再兜底盗版镜像并标注；`_optional_backend` 让免 key 的 OA 语料后端即使未配置也参与该链，`format` 为 `xml`/`html` 时换存盘后缀，XML 预览走 `fulltext.xml_to_text`）、`fetch book`（图书定位：搜索出候选 → `--pick` 选定补全书目 → 可选 `--download`；默认 zlibrary，`--backend doab` 走开放获取书源时提示语改为 “record (open access)”）、`export`（引文产出，arXiv key 归一为 DOI）、`backends list/status/authorize`（诊断/授权）、`completions <shell>`（生成 bash/zsh/fish 补全脚本）。

**补全实现（`litcli/completions.py`）**：签名/选项/取值/文件路径的补全 spec 一次性从 argparse parser 内省（`build_spec`），按 shell 渲染为静态表 + 小引擎；bash/zsh 用 `case` 表 + `_litcli_is_cmd` 走命令链 + COMPREPLY/`compadd`；fish 用声明式 `complete -c litcli`（`__fish_use_subcommand`/`__fish_seen_subcommand_from`）。文件路径补全的取值选项（`--pdf/--csl/--out`）与文件位置参数 (`data attach <key> <src>`) 用 `FILE_FLAGS`/`FILE_POSITIONALS` 硬编码；`--backend/--type` 取值用 `BACKENDS`/`TYPES` 常量。补全范围不含 key/query 位置参数（那需实时数据）。`models.AcademicItem` 现含 `container_title/volume/issue/page/publisher`，跨后端解析 `crossref`（container-title/volume/issue/page/publisher）、`openalex`（biblio + primary_location.source + doi 裸化）、`zotero_local`（publicationTitle/volume/issue/pages/publisher）填充，`export --format csljson/bibtex/ris` 输出完整期刊/卷/页。`data add` 的 `--creator` 用 `"家族, 名字"`，可多次；`--pdf` 抽元数据候选待核对、`--csl` 走 `from_csl`、`--force` 跳过查重/半残门。`data tree` 罗列集合树（层级显示）；`data members <集合key> [--no-recursive]` 罗列某集合条目（默认递归全部子集合，靠每条 `collections` 字段本地归属——Zotero `/items?collection=X` 实际被忽略返回全库）。

## 惯例与约定

- **一次改动只留一条提交**：审查、修复、文档补齐都先做完，再一次性提交；发现要改就 `commit --amend` 后 `push --force-with-lease`，不补 `fix:`/`style:` 类修补提交。顺序是审查 → 修复 → 全量测试 → 一次提交 → push；怕成果丢失用本地提交做检查点、审完 amend 合并再推。
- **所有 `.py` 文本一律英文**：注释、docstring，以及任何用户可见字符串（`print`/argparse `help`/`raise` 异常消息）都要英文——CLI 输出也走英文。只有用户文档保持中文：AGENTS.md、README.md、`skills/`、`config.example.toml`。标识符统一英文；按载体区分语言，不混用。
- 加后端：继承 `Backend`，实现 `search`/`get`，覆盖 `capabilities`（只读就 `{"read"}`），在 `config._registry()` 的 try-import 里注册，并在 `config.example.toml` 加字段。
- 新功能若要在领域层「输入某数据 → 输出统一模型」，优先做成纯函数（不 I/O、不联网），便于离线单测（现有测试都走构造数据、不连真库）。
- 写 `AcademicItem` 新增字段时，同步检查 `to_csl`/`from_csl`/`merge_items` 是否该感知该字段。
- `skills/litcli/SKILL.md` 是面向学术库互操作的技能**唯一源**（纯 markdown、不绑定 agent）；`install.sh` 按探测到的 agent 各自格式分发（Claude 技能目录 / Codex prompt / Cursor `.mdc` / Gemini 上下文标记块），并始终留一份中立副本 `~/.local/share/litcli/skill/SKILL.md`。加分发目标时改 `install.sh` 的 `install_skill` 分支，别再往源路径里嵌 agent 名。其命令表与 CLI 保持一致（其中「去重/排序暂不做」一句已过时——聚合层现已支持）。
