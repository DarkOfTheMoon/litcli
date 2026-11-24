---
name: litcli
description: 用 litcli 与用户的学术资料互操作——检索、读取、导出引文、读全文、新增/修改。覆盖本机 Zotero、arXiv、Crossref/OpenAlex、知网中文题录、豆瓣图书与开放获取语料（DOAJ / Europe PMC / DOAB / Zenodo / Sci-Hub / Z-Library）。当用户提到"我的文献/论文库/引用/参考文献/PDF/图书/书目"时使用。
---

# litcli 学术互操作（Zotero / arXiv / OA 语料）

命令行工具 `litcli`，后端无关：领域模型统一为 `AcademicItem`，Zotero 本地库可读可写，其余后端只读。

本技能是纯 markdown，不绑定某个 agent：出现 SKILL.md 目录树的地方按原样放；只吃全局上下文的，把它整篇并入上下文文件即可。

## 一条检索怎么走

1. **先看用户自己的库**：`litcli search "关键词" --backend zotero`（不带 `--backend` 时是全部来源一起搜，本机库只是其中之一）。
2. **库里没有才外扩**：`litcli search "关键词"` 默认就跨全部已配置来源 + 免 key 的 OA 语料聚合（去重/合并，`extra["sources"]` 标来源），或用 `--backend arxiv|crossref|openalex|doaj|europepmc|doab|zenodo` 指定开放源；中文文献用 `--backend cnki`，中文图书用 `--backend douban`。
3. **收敛**：`--type article` 降噪，`--title/--author/--year/--container` 字段级过滤，`--sort cited` 按被引降序。结果只是候选，**引用前核对题名/作者/年份**，不要把摘要当结论。
4. **顺藤摸瓜**：`refs`（回溯奠基）／`citing`（前向跟进）／`related`（语义邻居）——都基于 OpenAlex 引文图谱。
5. **落盘**：要入库走 `data add`（先查重、经确认）；只要引文走 `export --format csljson`（喂 `pandoc --citeproc`）；要全文走 `fetch <doi|arXiv id>`（默认落盘，stdout 只有路径；要正文且合法源拿不到时加 `--scihub`）。
6. **拿不到就说拿不到**：每条结论必须落到可追溯的条目（DOI / 来源 / key），不编造题录。

## 命令面（`litcli` 在 PATH；源码检出内用 `./bin/litcli`）

不写 `--backend` 时搜全部来源（含免 key 的 OA 语料）；`--backend` 收窄到单个，key 也可自带来源前缀
（`openalex/W123`、`zotero/AB12`）——有前缀按前缀路由，`--backend` 仍优先；`--all` 与 `--backend` 互斥，
聚合会去重/合并/排序并在 `extra["sources"]` 标来源。
命令按意图分族：`search`/`fetch`/`export` 只读，`data` 写库（须决策）。

| 任务 | 命令 | 读写 |
|---|---|---|
| 检索候选 | `litcli search "关键词" [--all] [--limit N] [--type article] [--title ..] [--author ..] [--year ..] [--container ..] [--sort cited]` | 只读 |
| 参考文献(回溯) | `litcli refs <key> [--limit N] [--backend openalex]` 该文所引文献,按被引降序(奠基在前) | 只读 |
| 引用该文(前向) | `litcli citing <key> [--limit N] [--backend openalex]` 引用该文的文献,按被引降序(跟进在前) | 只读 |
| 语义相关 | `litcli related <key> [--limit N] [--backend openalex]` OpenAlex `related_works` 邻居;该文无邻居列表时退回标题实词检索(`extra["related_via"]=keyword`);不支持的只读后端 `[skipped]` 退出码 2 | 只读 |
| 单条全字段 | `litcli data get <key> [--format table\|json\|csl]`（默认 `table`） | 只读 |
| 导出引文 | `litcli export <key> [--format csljson\|bibtex\|ris\|text]`（arXiv key 如 `arxiv/1806.09180` 在 `--backend openalex\|crossref` 下归一为 DOI；csljson/bibtex 含期刊名/卷/页/出版社） | 只读 |
| 定位全文 | `litcli fetch <doi\|arXiv id\|出版社文章 URL/PII> [--out DIR] [--no-download] [--scihub]`（旧写法 `fetch fulltext <doi>` 仍认；URL 形态覆盖 ScienceDirect / Wiley / Springer / Cambridge / AIP / ACM / APS / IOP / Taylor & Francis / AIAA / IEEE） 本地→OA→对应出版社订阅副本（按 DOI 前缀自动试，凭机构订阅；金 OA 免配置）默认落盘；加 `--scihub` 才兜底公开副本(会标注) | 只读 |
| **新增(元信息)** | `litcli data add --title "..." --creator "家族, 名字" --year 2002 [--doi ...]` | **写库** |
| **新增(从 PDF)** | `litcli data add --pdf <file>` 抽启发式候选→展示→**交互确认(y/N)**→写；`--yes` 跳过确认 | **写库** |
| 修改 | `litcli data set <key> --title "..." [--year ...]` | **写库** |
| 删除 | `litcli data del <key>` | **写库** |
| 挂全文附件 | `litcli data attach <key> <本地路径\|URL>` | **写库** |
| 罗列集合树 | `litcli data tree` 层级显示全部集合 | 只读 |
| 罗列某集合条目 | `litcli data members <集合key> [--no-recursive]` 默认递归全部子集合 | 只读 |
| 列后端 | `litcli backends list`（能力）· `litcli backends status`（授权/可达） | 只读 |
| 课题调查 | `litcli survey new "课题" [--seed DOI …] [--year A-B] [--type T] [--container 刊名] [--effort 档]` 建项目并打印项目名；种子与约束落 `project.toml`，不给 `--seed` 则先检索取前 3 条 | **建项目** |
| 课题续跑 | `litcli survey run <项目> [--effort 档] [--format jsonl\|md]` 沿引文图谱一跳一跳收网，每跳落检查点；stdout 是 JSONL 事件（`--format md` 换成按跳的表格），加到目标即续跑同一条命令 | 写 corpus |
| 课题状态 | `litcli survey status <项目>` 一行 JSON：课题/状态/跳数/准入数/最后停因/剩余额度 | 只读 |
| 课题停止 | `litcli survey stop <项目>` 写 `$.stop`，循环在下一个跳边界收尾；已停再停退 0 | 只读 |
| 订阅会话 | `litcli browser login <站点>` 在弹出的窗口里机构登录一次（浏览器独立运行、跨命令复用，之后无需搬 cookie；关窗口即结束）；`litcli browser status` 看各站是 signed in / signed out / gated / no answer | 只读 |
| 失败明细 | `litcli --verbose <命令>`：检索里哪个来源、什么原因失败（默认静默，只写 stderr） | 只读 |
| 追踪 | `litcli --debug <命令>`：请求/状态/耗时/重试到 stderr，排查后端为何失败 | 只读 |
| 装浏览器 | `litcli backends install-browser`（二进制安装包不带 Chromium；知网/豆瓣首次使用前跑一次）；装完用 `backends install-browser --check` 自检能否启动 | 下载 |
| 中文题录检索 | `litcli search --backend cnki "关键词"` 知网公开题录（需 Playwright + 可见浏览器；只取题录不抓全文） | 只读 |
| 豆瓣图书检索 | `litcli search --backend douban "书词"` 候选书单，`litcli data get --backend douban <subject-id>` 补全 ISBN/出版社/版次/页数（需 `install.sh --with-douban`；默认无头，只取公开书目元数据） | 只读 |
| 开放获取检索 | `litcli search --backend doaj\|europepmc\|doab\|zenodo "词"` 只搜开放授权语料（OA 期刊 / 生物医学+OA 全文 / OA 图书 / 开放数据集与软件）；免 key | 只读 |
| 图书定位/下载 | `litcli fetch --book "书名\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--limit N] [--pick N]` 候选→选定→补全书目（ISBN/出版社/版次/页数）→落盘。默认 Z-Library（影子图书馆，匿名每日下载有配额）；`--backend doab` 走 OA 书源；`--backend openlibrary` 仅书目 | 只读 |
| **首次写库授权** | `litcli backends authorize zotero` 弹框批准后写回 config | 写配置 |
| shell 补全 | `litcli completions <bash\|zsh\|fish>` 打印自包含补全脚本（`eval "$(litcli completions bash)"` 后 `litcli <TAB>` 补子命令/参数/取值/文件路径；补全时不起 `litcli` 子进程） | 只读 |

## 课题调查（survey）

一个课题是一个目录：`project.toml`（课题、种子、约束、阈值、档位）+ `seen.jsonl`（每条候选的分数分量与收/不收的理由）+ `trace.jsonl`（每步：扩展了谁、花了多少、余额、停因）+ `corpus/`（收下的文件与元数据）。全部追加式，可 grep、可 diff。

- **档位决定走多宽，不决定收多少条**：热的题自己走到额度或饱和，冷的题几跳没动静就自己停。`--effort low|medium|high|max`（默认 medium）：前沿/跳 4/8/16/24、准入/跳 2/5/8/12、邻居/条 10/20/30/50 等三个方向、连续无新高 2/3/4/6 跳即停、最长 6/20/50/120 跳。额度按请求计，命中缓存不扣、重试照扣。
- **停止是正常收尾**：`stop` 事件分别报 `saturated`（饱和）/ `budget`（发现来源额度用尽）/ `max_hops` / `stopped` / `interrupted`（SIGINT，状态可续）。续跑就是再跑一次 `run`：已展开过的条目不再展开。
- **一个项目同时只允许一个 `run`**（存在 `$.lock` 即退 2，先 `survey stop` 或确认进程已死再删锁）。
- **拒绝也写理由**：`seen.jsonl` 每行带 `reason`（如 `score 3.21 >= 2.50`、`seen`、`year … outside …`）。下载失败只标 `unavailable`，候选与元数据都留着。
- `run` 的退出码：`0` 至少落下一份正文副本、`1` 一份都没落下（只剩元数据旁车也算没落下）、`2` 出错或项目已在跑。

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
- **检索失败默认静默**：单个来源挂了不提（`--verbose` 才逐条说明）；全部来源都失败会明说并退出码 2。因此退出码 0 且「没结果」要先想「是不是全挂了」，怀疑时补 `--verbose` 重跑。`fetch`/`data`/`export`/`refs`/`citing`/`related` 出错一律明说。`fetch` 的退出码另分三档：`0` 拿到文件、`1` 没有全文、`2` 出错——`1` 不是错误，别当故障重试。
- **CNKI 只取公开题录、不抓全文**：知网无开放 API；`search --backend cnki` 需先 `./install.sh --with-cnki` 装 Playwright + Chromium。它会弹**可见浏览器**，请你手动过滑块验证码 + 登录知网（校外账号）一次（独立 profile，之后复用）。检索结果仅标题/作者/刊名/年份/摘要片段，**不下载付费墙全文**；遇验证码交给人工，脚本不绕过、不实现反爬规避。
- **开放获取源只给公开 OA 记录**：DOAJ / DOAB / Europe PMC / Zenodo 均免 key、开放授权；`fetch` 在合法链里用 Europe PMC（JATS XML 文本，存 `.xml`）与 Zenodo（开放文件），Unpaywall 只给落地页时退到链末并标 `HTML`。
- **被 403 不等于你的 IP 被封**：`europepmc.org` 在 Cloudflare managed challenge/Turnstile 后面（CLI、假浏览器头、无头/有头浏览器实测都过不去，故走 EBI 的 REST）；DOAB 的 `/rest` 对无 JS 客户端 403（故走 Atom feed），但它的详情页是可解的工作量证明，litcli 本地解、换 cookie 后正常取页。这两类站点都无需换网络/代理。
- **ScienceDirect / Wiley 走你的机构订阅**：`fetch` 对 `10.1016`、`10.1002` 等 Elsevier / Wiley DOI 各自动试一次，拿到的是订阅全文（不是 OA 授权，输出标注 `entitled`）。需要机构会话：校内 IP 直接可用；校外则在**已认证**的浏览器标签里取 Cookie 与其 UA 配进 `[backends.sciencedirect]` / `[backends.wiley]`（`cookie`/`cookie_file` + `user_agent` 必须成对，绑 UA+出口 IP、服务端随时可能失效；未认证页只给 Purchase PDF）。未获授权时给 Cloudflare 挑战页（`CPE00001` 等），链条继续往下走，不阻断；PDF 另有验收关（SD 走 `craft/challenge/pdf`，Wiley 未订阅时 PDF 端点跳摘要页），被拦时 `fetch` 先走完链尾（落地页、`--scihub`）再报因给链接。Wiley 可另配 `tdm_token`（免费申请）：走 `api.wiley.com` 不过 Cloudflare，只放行机构已订阅文章，否则 403，但页面路径仍会照试。
- **SpringerLink 没有 Cloudflare**：文章页匿名可读、题录恒在，金 OA 的 PDF 匿名直取；只有付费文章的 PDF 要会话（端点先 303 到 `idp.springer.com` 换身份 cookie，未订阅会落回文章页），同样配 `[backends.springer]` 的 `cookie` + `user_agent` 成对使用。
- **Cambridge / AIP 同样走机构订阅**：`fetch` 对 `10.1017`（Cambridge）、`10.1063` / `10.1121`（AIP）各自动试一次。Cambridge 无 Cloudflare，匿名页题录恒在、CC 许可 OA 的 PDF 匿名直取，付费条目只到 `/abs/` 短页、PDF 端点回跳条目页；AIP 在 Cloudflare 挑战后，几乎必须配 `[backends.aip]` 的 `cookie` + `user_agent`。两者都只取机构已订阅或 OA 的正文，单 DOI、低频。
- **ACM / APS / IOP / Taylor & Francis / AIAA / IEEE 同属一道闸**：前五家与 AIP 一样在 Cloudflare 托管挑战后（`fetch` 分别对
  `10.1145` / `10.1103` / `10.1088` / `10.1080` / `10.2514` 自动试一次），IEEE（`10.1109`）是 WAF JS 挑战、靠 `aws-waf-token`（另有 F5 的 `TS*` 拦截页）。
  六家都必须成对配 `cookie` + `user_agent`，未配会话时报的就是这道门。清关 cookie 是 HttpOnly（`cf_clearance` /
  `aws-waf-token` / `TS*`），**手抄 Cookie 头很容易漏掉**：更稳的是用浏览器扩展/`curl` 导出 Netscape `cookies.txt` 原样指给
  `cookie_file`，导出里的 `#HttpOnly_` 行 litcli 会读。
- **不想搬 cookie 就用共享浏览器会话**（`[browser].enabled = true`，默认关闭）：装好 Chrome，`litcli browser login <站点>` 在弹出的窗口里机构登录一次，浏览器保持运行；此后 `fetch` 对这些出版社在页内取页与 PDF，凭据不出浏览器、过期自续，`litcli browser status` 逐站报 `signed in` / `signed out` / `gated` / `no answer`。关着时逐字走 cookie 路径，配了 cookie 照样能工作；浏览器自动沿用 `[network].proxy`（清关绑出口 IP，两边须同一出口）。
- **Sci-Hub 是可选兜底、默认不介入**：`fetch` 仍先走本地库/Unpaywall/OpenAlex，只有加 `--scihub`（或配置启用）才试公开副本；输出标 `source=scihub` 且注明非 OA 授权。镜像用 ALTCHA 验人，litcli 用本地 `hashlib` 解同一道题（不绕过），每次单 DOI、低频。
- **Z-Library 给的是公开副本（非 OA 授权）**：`fetch --book` 用来找 Sci-Hub 没有的图书（Sci-Hub 只按 DOI 解析论文）。无需 Playwright，但用前要过站点“Checking your browser”工作量证明——litcli 用本地 `hashlib` 解同一道题（不绕过）。**匿名每日下载有配额**，超限会明确提示，可在 `[backends.zlibrary]` 配 `email`/`password` 登录放宽。只取公开书目元数据与公开 PDF 直链。
- **豆瓣只取公开书目元数据、不绕过反爬**：豆瓣无公开 API；`search --backend douban` 也需 `install.sh --with-douban` 装 Playwright + Chromium，但**默认无头**——搜索页 SSR 直接给书卡 JSON，无需登录/滑块。仅当被风控时手动在可见浏览器处理，脚本不程序化绕过。图书以 ISBN 为身份键（无 DOI），`search --all` 聚合时与期刊/论文的 DOI 身份键并行。
