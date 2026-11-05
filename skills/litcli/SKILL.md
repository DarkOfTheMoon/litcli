---
name: litcli
description: 用 litcli 与用户的学术资料互操作——检索、读取、导出引文、读全文、新增/修改。覆盖本机 Zotero、arXiv、Crossref/OpenAlex、知网中文题录、豆瓣图书与开放获取语料（DOAJ / Europe PMC / DOAB / Zenodo / Sci-Hub / Z-Library）。当用户提到"我的文献/论文库/引用/参考文献/PDF/图书/书目"时使用。
---

# litcli 学术互操作（Zotero / arXiv / OA 语料）

命令行工具 `litcli`，后端无关：领域模型统一为 `AcademicItem`，Zotero 本地库可读可写，其余后端只读。

本技能是纯 markdown，不绑定某个 agent：出现 SKILL.md 目录树的地方按原样放；只吃全局上下文的，把它整篇并入上下文文件即可。

## 一条检索怎么走

1. **先查用户自己的库**：`litcli search "关键词"`（默认后端就是本机 Zotero，不需要 `--backend`）。
2. **库里没有才外扩**：`--all` 跨后端聚合（去重/合并，`extra["sources"]` 标来源），或用 `--backend arxiv|crossref|openalex|doaj|europepmc|zenodo` 指定开放源；中文文献用 `--backend cnki`，中文图书用 `--backend douban`。
3. **收敛**：`--type article` 降噪，`--title/--author/--year/--container` 字段级过滤，`--sort cited` 按被引降序。结果只是候选，**引用前核对题名/作者/年份**，不要把摘要当结论。
4. **顺藤摸瓜**：`refs`（回溯奠基）／`citing`（前向跟进）／`related`（语义邻居）——都基于 OpenAlex 引文图谱。
5. **落盘**：要入库走 `data add`（先查重、经确认）；只要引文走 `export --format csljson`（喂 `pandoc --citeproc`）；要全文走 `fetch fulltext <doi>`。
6. **拿不到就说拿不到**：每条结论必须落到可追溯的条目（DOI / 来源 / key），不编造题录。

## 命令面（`litcli` 在 PATH；源码检出内用 `./bin/litcli`）

后端经 `--backend`（默认 `zotero`），`--all` 跨后端聚合（去重/合并/排序，`extra["sources"]` 标来源）。
命令按意图分族：`search`/`fetch`/`export` 只读，`data` 写库（须决策）。

| 任务 | 命令 | 读写 |
|---|---|---|
| 检索候选 | `litcli search "关键词" [--all] [--limit N] [--type article] [--title ..] [--author ..] [--year ..] [--container ..] [--sort cited]` | 只读 |
| 参考文献(回溯) | `litcli refs <key> [--limit N] [--backend openalex]` 该文所引文献,按被引降序(奠基在前) | 只读 |
| 引用该文(前向) | `litcli citing <key> [--limit N] [--backend openalex]` 引用该文的文献,按被引降序(跟进在前) | 只读 |
| 语义相关 | `litcli related <key> [--limit N] [--backend openalex]` OpenAlex `related_works` 邻居;不支持的只读后端 `[skipped]` 退出码 2 | 只读 |
| 单条全字段 | `litcli data get <key> [--format csl\|json]` | 只读 |
| 导出引文 | `litcli export <key> [--format csljson\|bibtex\|ris\|text]`（arXiv key 如 `arxiv/1806.09180` 在 `--backend openalex\|crossref` 下归一为 DOI；csljson/bibtex 含期刊名/卷/页/出版社） | 只读 |
| 定位全文 | `litcli fetch fulltext <doi> [--download]` 本地→OA；加 `--scihub` 才兜底盗版镜像(会标注) | 只读 |
| **新增(元信息)** | `litcli data add --title "..." --creator "家族, 名字" --year 2002 [--doi ...]` | **写库** |
| **新增(从 PDF)** | `litcli data add --pdf <file>` 抽启发式候选→展示→**交互确认(y/N)**→写；`--yes` 跳过确认 | **写库** |
| 修改 | `litcli data set <key> --title "..." [--year ...]` | **写库** |
| 删除 | `litcli data del <key>` | **写库** |
| 挂全文附件 | `litcli data attach <key> <本地路径\|URL>` | **写库** |
| 罗列集合树 | `litcli data tree` 层级显示全部集合 | 只读 |
| 罗列某集合条目 | `litcli data members <集合key> [--no-recursive]` 默认递归全部子集合 | 只读 |
| 列后端 | `litcli backends list`（能力）· `litcli backends status`（授权/可达） | 只读 |
| 中文题录检索 | `litcli search --backend cnki "关键词"` 知网公开题录（需 Playwright + 可见浏览器；只取题录不抓全文） | 只读 |
| 豆瓣图书检索 | `litcli search --backend douban "书词"` 候选书单，`litcli data get --backend douban <subject-id>` 补全 ISBN/出版社/版次/页数（需 `install.sh --with-douban`；默认无头，只取公开书目元数据） | 只读 |
| 开放获取检索 | `litcli search --backend doaj\|europepmc\|doab\|zenodo "词"` 只搜开放授权语料（OA 期刊 / 生物医学+OA 全文 / OA 图书 / 开放数据集与软件）；免 key | 只读 |
| 图书定位/下载 | `litcli fetch book "书名\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--limit N] [--pick N] [--download]` 候选→选定→补全书目（ISBN/出版社/版次/页数），可选下载文件。默认 Z-Library（影子图书馆，匿名每日下载有配额）；`--backend doab` 走 OA 书源；`--backend openlibrary` 仅书目 | 只读 |
| **首次写库授权** | `litcli backends authorize zotero` 弹框批准后写回 config | 写配置 |
| shell 补全 | `litcli completions <bash\|zsh\|fish>` 打印自包含补全脚本（`eval "$(litcli completions bash)"` 后 `litcli <TAB>` 补子命令/参数/取值/文件路径；补全时不起 `litcli` 子进程） | 只读 |

## 环境事实

- Zotero 10 本机运行于 `localhost:23119`，本地 API 免 key（`users/0` 别名→真实库）；写请求需 `Zotero-Server-ID` + `If-Unmodified-Since-Version`（工具已自动处理）。
- **写库前先授权一次**：`litcli backends authorize zotero` → 本地弹框批准 → key 写入 `[backends.zotero].api_key`，此后写操作自动携带、不再弹框。未授权时 `data add/set/del/attach` 报 `HTTP 401`（退出码 2）。
- Better BibTeX 未装：引文走 `export --format csljson` 喂 `pandoc --citeproc`，不依赖 BBT。
- 读 PDF 全文需系统 `pdftotext`。除 zotero 外都是只读后端，`data` 写操作一律报 `[skipped]`（退出码 2）；想确认某后端能不能写，看 `litcli backends list` 的能力列。
- arXiv 按 IP 限速：连发可能 `HTTP 429 Rate exceeded.`（已退避重试）；检索若失败，隔几秒再试，或给 `[backends.arxiv]` 配 `mailto`。

## 注意

- `data add` 返回新 key；写库仅对可写后端（zotero）成功，其余报错并退出码 2。
- `data add` 写前过 `should_import` 查重/半残门：同 DOI/题已存在、或缺标题/缺作者且缺年份 → 拒绝，需 `--force` 覆盖。
- `data add --pdf` 是**启发式**候选，写前会交互确认（y/N）请核对；非交互/脚本用 `--yes` 跳过确认。勿盲目入库。
- 无 `--all` 时只查默认后端（zotero）；要跨库检索必须显式 `--all` 或逐个 `--backend`。
- **CNKI 只取公开题录、不抓全文**：知网无开放 API；`search --backend cnki` 需先 `./install.sh --with-cnki` 装 Playwright + Chromium。它会弹**可见浏览器**，请你手动过滑块验证码 + 登录知网（校外账号）一次（独立 profile，之后复用）。检索结果仅标题/作者/刊名/年份/摘要片段，**不下载付费墙全文**；遇验证码交给人工，脚本不绕过、不实现反爬规避。
- **开放获取源只给公开 OA 记录**：DOAJ / DOAB / Europe PMC / Zenodo 均免 key、开放授权；`fetch fulltext` 在合法链里用 Europe PMC（JATS XML 文本，存 `.xml`）与 Zenodo（开放文件），Unpaywall 只给落地页时退到链末并标 `HTML`。
- **被 403 不等于你的 IP 被封**：`europepmc.org` 在 Cloudflare managed challenge/Turnstile 后面（CLI、假浏览器头、无头/有头浏览器实测都过不去，故走 EBI 的 REST）；DOAB 的 `/rest` 对无 JS 客户端 403（故走 Atom feed），但它的详情页是可解的工作量证明，litcli 本地解、换 cookie 后正常取页。这两类站点都无需换网络/代理。
- **Sci-Hub 是可选兜底、默认不介入**：`fetch fulltext` 仍先走本地库/Unpaywall/OpenAlex，只有加 `--scihub`（或配置启用）才试盗版镜像；输出标 `source=scihub` 且注明非 OA 授权。镜像用 ALTCHA 验人，litcli 用本地 `hashlib` 解同一道题（不绕过），每次单 DOI、低频。
- **Z-Library 是影子图书馆（非 OA 授权）**：`fetch book` 用来找 Sci-Hub 没有的图书（Sci-Hub 只按 DOI 解析论文）。无需 Playwright，但用前要过站点“Checking your browser”工作量证明——litcli 用本地 `hashlib` 解同一道题（不绕过）。**匿名每日下载有配额**，超限会明确提示，可在 `[backends.zlibrary]` 配 `email`/`password` 登录放宽。只取公开书目元数据与公开 PDF 直链。
- **豆瓣只取公开书目元数据、不绕过反爬**：豆瓣无公开 API；`search --backend douban` 也需 `install.sh --with-douban` 装 Playwright + Chromium，但**默认无头**——搜索页 SSR 直接给书卡 JSON，无需登录/滑块。仅当被风控时手动在可见浏览器处理，脚本不程序化绕过。图书以 ISBN 为身份键（无 DOI），`search --all` 聚合时与期刊/论文的 DOI 身份键并行。
