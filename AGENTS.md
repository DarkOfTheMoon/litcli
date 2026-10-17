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
  - `models.AcademicItem` 是唯一归一化模型（`source/key/title/creators/year/abstract/doi/url/item_type/attachments/extra`），与 CSL JSON 双向转换（`to_csl` / `from_csl`）、可导 BibTeX。
  - `citation.py` 提供零依赖的 author-date in-text / 参考文献简版渲染；正统路径是 `export --format csljson` 喂 `pandoc --citeproc`，**不依赖 Better BibTeX**。
  - `pdf.py`：下载 + `pdftotext` 抽文本，后端无关的「拿到 bytes/路径 → 抽文本」。
  - `metadata.py`：纯启发式从 PDF 首页文本猜 title/authors/year/abstract，**不调 LLM、完全离线**；`from_text_lines` 是纯函数（可脱离 pdftotext 复用）。`data add --pdf` 用它抽候选、**展示待人工核对**后才写库，不自动写。

- **聚合层**（`litcli/aggregate.py`，纯函数、不碰后端不联网）：供 `search --all` 用的去重/合并/排序。身份键**优先生效 DOI**、无 DOI 退化为归一标题（`norm_title` / `norm_doi`）；`norm_doi` 剥 `https://doi.org/` 前缀——Crossref 给裸 DOI、OpenAlex 给完整 URL，不剥前缀会让同一文献身份键各异而无法去重；`_merge_two` 只把缺失字段（含 container_title/volume/issue/page/publisher/cited_by_count 及 extra["openalex_id"]）补给最完整记录（副本操作，不污染入参，见 `test_does_not_mutate_input`）；`extra["sources"]` 记录全部来源；排序按年份降序、同年标题升序。注意：预印本与刊出本是两个不同 DOI/作品，语义上不应合并。

- **后端层**（`litcli/backends/`）：`base.py` 定义抽象 `Backend` + `capabilities`（`read`/`write`/`attachment`）。写操作 `add`/`patch`/`delete`/`attach` 内部先 `_require("write")`，只读后端抛 `NotSupportedError`（CLI 捕获后输出 `[跳过]` 并以退出码 2 返回）。
  - `zotero_local.py`：Zotero 10 本地 API（`localhost:23119/api`，`users/0` 本机别名，**无需 zotero.org Web API key**）。读+写+附件，写入用 `If-Unmodified-Since-Version` 并发控制，写请求需 `Zotero-Server-ID`（从 `/api` 响应头缓存，非 `/connector/ping` 的 body——后者是 HTML）。`search` 用 `qmode=everything`（默认 `titleCreatorYear` 漏摘要字段，中文/摘要词命中需此模式）；`ping()` 供 `backends status` 轻量探测本机库是否在运行。写库 key 经 `litcli backends authorize zotero` 调 `/api/local/authorize` 运行时授权并写回 `config.toml` 的 `[backends.zotero].api_key`；`delete` 走 DELETE，`attach` 挂 link 模式附件（本地→linked_file，URL→linked_url）。
  - `arxiv.py`：arXiv 开放 API（Atom/XML），只读 + 可拉 PDF 直链，按 arXiv id（去 `v` 版本）归一化。
  - `crossref.py` / `openalex.py`：开放元数据（DOI / 学术图谱），只读，5xx 指数退避重试；OpenAlex 摘要为倒排索引需重建。`search(query, limit, filters)` 把 `--title/--author/--year/--container` 映射为各后端过滤词（crossref → `query.title/author/container-title` + `filter=from-pub-date`；openalex → `filter=title.search/raw_author_name.search/publication_year`，container 折叠进自由 `search`；arxiv → `ti:`/`au:`/`submittedDate` AND 语法；zotero → 折进单 `q`）。`_work_to_item`（openalex）填 `cited_by_count` + `extra["openalex_id"]`。引文图谱三方向为本后端独占能力（base 默认 `NotSupportedError`）：`related(key, limit)` 读父 work 内联 `related_works` 逐个取全量；`references(key, limit)` 读内联 `referenced_works` 并行拉取、按被引降序（预算 `max(limit,50)`）；`citing(key, limit)` 走 `filter=cites:<W-id>&sort=cited_by_count:desc` 单请求拿全量。`related`/`references` 共用 `_inline_works`（父 work 内联 W-id 列表 → `_fetch_works` 并行拉取、按被引降序，`budget` 限一次最多取多少；`citing` 不走它）。
  - `openlibrary.py`：开放图书（`search.json`），只读；**默认内置不含**（部分网络无路由），在 config 显式启用。
  - `uspto.py`：USPTO 专利全文检索，只读 + **key 门控**（免费注册）；免 key 的 Crossref/OpenAlex 公开版专利记录趋近 0，专利以本后端为主通道。

- **配置层**（`litcli/config.py`）：读 `~/.config/litcli/config.toml`（不支持则在 `$LITCLI_CONFIG`），`tomllib` 解析。加载优先级：显式 path → `$LITCLI_CONFIG` → `~/.config/litcli/config.toml` → **内置默认**（Zotero + arXiv + crossref + openalex）。写 Zotero 经 `litcli backends authorize zotero` 触发 `/api/local/authorize` 运行时授权并把本地 key 写回 `[backends.zotero].api_key`（`config.write_backend_key`，手写文本 upsert 保其它段）；检索专利（uspto）需在 config 配真实 `api_key`。后端注册表用 try-import：某后端文件未就绪时不影响其它后端工作。配置示例见 `config.example.toml`。

- **CLI 编排层**（`litcli/cli.py`）：argparse 意图分族子命令：`search`（检索，`--type` 按类型过滤、`--title/--author/--year/--container` 字段级过滤、`--sort cited` 按被引降序）、`refs`/`citing`/`related`（引文图谱三方向——参考文献回溯/引用前向/语义邻居，默认 openalex，不支持的只读后端 `[跳过]` 退出码 2）、`data get/add/set/del/attach/tree/members`（本地库增删改查 + 集合罗列）、`fetch fulltext`（合法全文定位）、`export`（引文产出，arXiv key 归一为 DOI）、`backends list/status/authorize`（诊断/授权）、`completions <shell>`（生成 bash/zsh/fish 补全脚本）。

**补全实现（`litcli/completions.py`）**：签名/选项/取值/文件路径的补全 spec 一次性从 argparse parser 内省（`build_spec`），按 shell 渲染为静态表 + 小引擎；bash/zsh 用 `case` 表 + `_litcli_is_cmd` 走命令链 + COMPREPLY/`compadd`；fish 用声明式 `complete -c litcli`（`__fish_use_subcommand`/`__fish_seen_subcommand_from`）。文件路径补全的取值选项（`--pdf/--csl/--out`）与文件位置参数 (`data attach <key> <src>`) 用 `FILE_FLAGS`/`FILE_POSITIONALS` 硬编码；`--backend/--type` 取值用 `BACKENDS`/`TYPES` 常量。补全范围不含 key/query 位置参数（那需实时数据）。`models.AcademicItem` 现含 `container_title/volume/issue/page/publisher`，跨后端解析 `crossref`（container-title/volume/issue/page/publisher）、`openalex`（biblio + primary_location.source + doi 裸化）、`zotero_local`（publicationTitle/volume/issue/pages/publisher）填充，`export --format csljson/bibtex/ris` 输出完整期刊/卷/页。`data add` 的 `--creator` 用 `"家族, 名字"`，可多次；`--pdf` 抽元数据候选待核对、`--csl` 走 `from_csl`、`--force` 跳过查重/半残门。`data tree` 罗列集合树（层级显示）；`data members <集合key> [--no-recursive]` 罗列某集合条目（默认递归全部子集合，靠每条 `collections` 字段本地归属——Zotero `/items?collection=X` 实际被忽略返回全库）。

## 惯例与约定

- **所有 `.py` 文本一律英文**：注释、docstring，以及任何用户可见字符串（`print`/argparse `help`/`raise` 异常消息）都要英文——CLI 输出也走英文。只有用户文档保持中文：AGENTS.md、README.md、`.claude/skills/`、`config.example.toml`。标识符统一英文；按载体区分语言，不混用。
- 加后端：继承 `Backend`，实现 `search`/`get`，覆盖 `capabilities`（只读就 `{"read"}`），在 `config._registry()` 的 try-import 里注册，并在 `config.example.toml` 加字段。
- 新功能若要在领域层「输入某数据 → 输出统一模型」，优先做成纯函数（不 I/O、不联网），便于离线单测（现有测试都走构造数据、不连真库）。
- 写 `AcademicItem` 新增字段时，同步检查 `to_csl`/`from_csl`/`merge_items` 是否该感知该字段。
- `.claude/skills/zotero/SKILL.md` 是面向 Zotero/arXiv 库互操作的技能，其命令表与 CLI 保持一致（其中「去重/排序暂不做」一句已过时——聚合层现已支持）。
