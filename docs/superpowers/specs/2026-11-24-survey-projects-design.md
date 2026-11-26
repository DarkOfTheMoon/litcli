# 课题项目与自主收网

日期：2026-11-24 · 状态：待批准

## 缘起

三处具体的疼，指向同一个缺：litcli 是无状态的。一条命令进来、一条结果出去，什么都不留。

1. 取订阅全文靠人手工搬 cookie，一过期就得重来，而清关 cookie 同时绑 UA 与出口 IP、分钟到小时级失效——搬的动作本身注定会反复做。
2. 针对一个特定课题做研究时，没有「项目」，没有「会话」，也不存中间结果；下一次检索跟前一次毫无关系。
3. litcli 应该成为 agent 手里最得力的工具，可现在只给了检索和下载，没有主动沿图谱收网、把关键文献捞出来的能力。

结论：litcli 需要**一个能存东西的地方**（项目），以及**在项目上自主跑的研究循环**（收网）。凭据是同一个地方的另一面。

## 目标

- 订阅站点不再手工搬 cookie：真实浏览器常驻会话，页内取件。
- 一个课题 = 一个项目，问题、约束、看过什么、留下什么、为什么留，全都在里面，随时可读回、可续跑。
- agent 给课题，litcli 自己沿引用图一圈圈收，产出带出处理由的语料。
- 收网可中断、可续跑；投入按档位（低/中/高/最大），实际收多少由课题热度自己决定。

## 非目标

- 不代写综述文字。产物是语料 + 线索，写作归调用方。
- 收网期间不写 Zotero；入库是另一个显式动作。
- 不做通用爬虫，不从任意 URL 起爬；种子只来自查询与显式 `--seed`。
- 不上数据库。

## 反目标（做了也算失败）

- 循环能悄悄烧掉无上限的请求和时长。
- 候选被收进语料却没有落下来的理由，事后没人能解释它为什么在。
- 为这件事让单发的 `search` / `fetch` / `export` 依赖项目才能跑。
- 把离线测试拖成需要联网。

## 约束

- 全部新增网络路径沿用现有设施：24 小时元数据缓存、`--debug` / `--verbose`、带分类的错误、礼貌池邮箱。
- 发现走开放 API（OpenAlex 为主），只有下载才需要订阅会话。
- 额度按档位落到站，且低于该站限速（arXiv / OpenAlex 按 IP 退避，出版社单 DOI 低频）。
- 老的单发动词行为不变（含退出码）。

## 方案（已选 A）

被订阅站点那一步怎么取件，三个真选项：

- **A. 真实 Chrome 常驻会话、页内取件。** 复用 `litcli/backends/playwright.py:78` 已有的 `launch_persistent_context`，`channel` 指向本机 `google-chrome`，一个**所有出版站共用**的 profile，默认有头。你做一次机构登录，之后 litcli 在该页面上下文里导航取件，从不把 cookie 搬进 httpx。凭据由活着的浏览器自己续，UA/IP 绑定天然成立——2026-11-24 实测唯一能过 ScienceDirect 的形态。代价：必须有浏览器在跑，锁与端口自己管（Chrome ≥136 起调试端口必须配独立 `--user-data-dir`），页内取件要把状态码/重试/缓存搬过去。
- B. 坚持搬 cookie，只把会话做成对象（从 Chrome cookie 库或 `cookies.txt` 读、探失效、按站点存）。无需浏览器、headless 友好；但过期仍要人再搬，Linux 走 keyring、Windows 走 DPAPI，读取本身脆。
- C. 混合：A 为主、cookie 当退路。两条路径都要维护，行为会漂。

**选 A**，并把「页内取件、不抽 cookie」定成纪律，过期与绑定两个问题一起消掉。

页面本身也走浏览器：闸在页面上，不在 PDF 端点，只把下载搬过去等于白搬。现有的 cookie 通路**原样保留**作为退路——浏览器不可用时逐字退回今天的 `_gated` 行为，11 个出版社的既有 stub 用例因此继续有效，不需要重写。两条路径的优先次序写在 `_entitled` 的一处判定里。

`context.close()` 之后就退出的老做法（`playwright.py:92`）保留给「本命令内用一次」的场景；常驻会话由 `browser login` 起，起了就**不关**，后续命令附着到它。

## 设计

### 项目仓库

`paths.project_root()` → `$XDG_DATA_HOME/litcli/projects/<slug>/`（Windows `%LOCALAPPDATA%\litcli\projects`）。`<slug>` 由课题生成、`safe_filename` 清洗，冲突加序号。

```
project.toml     课题、种子、约束、权重、档位、状态、时间戳
seen.jsonl       每个候选一条：来源、hop、分数分量、收或不收及理由
trace.jsonl      每步一条：扩展了谁、花了多少、当前额度余额、停因
corpus/          收下的文件（`safe_filename` 命名）+ 同名 .json 元数据
```

追加式 JSONL，agent 可 grep、可 diff。写入一律先落 `tmp` 再 `rename`，进程被杀不留半行。

### 打分

每个候选落 `seen.jsonl` 时带全部分量，可复算：

- **(c) 簇亲和（骨架）**：与项目已收集合的共被引数（OpenAlex `referenced_works` 交集）、`related` 语义邻居、同刊命中。
- **(q) 题目（锚）**：候选自身（题名、摘要）携带多少课题词。`c` 只说明「像已收下的东西」，集子一旦起漂就拉着自己的邻居一起漂；`q` 是不跟着漂的那根锚，也是首跳与只有题名的记录唯一可用的轴。
- **(b) 时间（拉维度）**：年份在窗口内的归一化位置；引用种子的额外加分（前沿）。
- **(a) 经典（兜底）**：被引数按年龄归一；只在簇稀疏（近三跳新增低于阈值）时提权。
- **(d) 类型**：`project.toml` 里声明偏好（review / method / empirical），乘权重。

准入分两问，同一个候选上依次判：**能不能进**由**证据**（`0..1`：被集内引用、共享参考文献按候选自身文献量归一、题目按腿取 `minimum_should_match` 后的最弱腿（三腿取二，四腿以上 75%））对阈值 `--floor`（默认 0.2，挡垃圾）与约束过滤、`aggregate.identity` 未见决定；**这一跳让谁进**由档位的单跳准入数按证据从高到低截取，证据相同才看总分、再相同按到达顺序——先答上的源不等于答得最好的。跳内所有候选对同一份「跳首集子」打分。**拒绝也要写理由**（被截掉的记 `rank k of n for cap c`）。

准入只读证据而不读总分，因为 `b`/`a`/`d` 三轴恒在、与课题无关，满分之和恰好 2.5（簇稀疏时 3.5）：以总分设闸，任何近期高被引的普通文章都能压线进。地板定在「一个实词」之下、「两个实词」之上，否则首跳（还没有簇，只能靠题目轴）一篇都收不进。

打分要的原始数据现在到不了模型：`openalex._work_to_item` 把 OpenAlex 的 `type` 折进 `_TYPE_MAP`（`review` → `article`，`openalex.py:23-30`），`referenced_works` 也没保留（只有 `extra["openalex_id"]`，`:134`）。因此该后端补两项：`extra["openalex_type"]` 原样带出（`--type` 收 OpenAlex 原词，显示时才映射回 CSL 词汇），`extra["referenced_works"]` 存进候选记录，供共被引打分与事后复算，不再回源。

约束过滤：`--year` 是硬过滤，种子豁免；`--type` 只是权重不是过滤，另给 `--container` 做硬过滤（刊名/会议名子串）匹配，因为「换一本刊」常比「换个类型」更常被用来收窄。

### 下载

复用 `fetch` 的全文链，`--scihub` 仅当配置里开着才走；无 DOI 的候选（只有 W-id）照常打分但不下载，记录标 `no-doi`。下载失败不剔除候选：元数据留在 `corpus/` 里、标 `unavailable`，并继续参与簇打分——花了预算就该留下东西。每条文件记 `access` 与落地方式，同 `fetch` 的 provenance 规则。

发现的检索与扩展只走 OpenAlex（唯一带图谱的来源）；种子可以来自任何已配置后端的检索，但必须能归一出 DOI 或 W-id。

### 循环

一跳 = 取前沿前 N 条 → 沿启用轴扩展（每条 refs/citing/related 按档取邻居）→ 去重打分 → 准入者走全文链下载进 `corpus/` → 写 `trace.jsonl` → 落检查点。N 与单跳准入数由档位给。

**预算是档位，不是条数。** 收进来多少条取决于课题热度：热的题每跳都有新高分，就一直跑到额度或饱和；冷的题几跳没动静就自己停。`--effort low|medium|high|max`（默认 medium）：

| 档 | 前沿/跳 | 准入/跳 | 邻居/条 | 连续无新高即停 | openalex 上限 | arxiv 上限 | 每出版社 | 最长跳 |
|---|---|---|---|---|---|---|---|---|
| low | 4 | 2 | refs 10 / citing 10 / related 5 | 2 | 40 | 10 | 2 | 6 |
| medium | 8 | 5 | 20 / 20 / 10 | 3 | 200 | 40 | 6 | 20 |
| high | 16 | 8 | 30 / 30 / 15 | 4 | 600 | 100 | 15 | 50 |
| max | 24 | 12 | 50 / 50 / 25 | 6 | 1500 | 200 | 30 | 120 |

命中缓存不扣额度，重试扣。在 `project.toml` 里可手改单项；`--effort` 重设整张表。

种子在 `survey new` 时无条件准入并构成初始簇；没给 `--seed` 就跑一次检索、取前 3 条当种子（标记 `seed=search`）。「新高分」的基准是**历史最高准入分**，不是本跳最高。

停止：饱和（连续无新高到档位值，这是正常收尾）/ 发现来源额度用尽（openalex 是唯一的发现来源，它用完即整轮结束；出版社名额用完只在下载侧降级为只留元数据）/ 跳数到顶 / `survey stop` / SIGINT（留可续状态）。前三种在 `stop` 事件里分开报 `reason`。续跑用同一条命令，从 `trace.jsonl` 最后一个检查点重建前沿，已展开过的条目不再展开。

同一项目第二个 `run` 拒绝启动（退出 2，提示先 `stop`）；`stop` 只写 `$.stop` 哨兵文件，由循环在跳边界读到。

### 浏览器会话层

新模块 `litcli/browser.py`：共用 profile 在 `$XDG_DATA_HOME/litcli/browser-profile`，`channel="chrome"`，有头，单例锁 + `--remote-debugging-port=0` 的发现文件。

- `litcli browser login [site]`：开窗到该站登录页，你登完回车。
- `litcli browser status`：逐站探登录态（未登录 / 已登录 / 站点在闸后）。
- 出版社后端的 `pdf(doi)` 保留各自的 URL 知识，取件改为交给会话对象在页面里导航并取响应体；transport 的缓存/重试/错误分类套在外层。

### 命令面与 agent 契约

```
litcli survey new "<课题>" [--seed DOI]... [--year A-B] [--type review] [--container NAME] [--effort low|medium|high|max]
litcli survey run <项目> [--effort LEVEL] [--format jsonl|md]
litcli survey status <项目>
litcli survey stop <项目>
```

stdout 默认就是一行一条的 JSONL 事件（`event` 字段：`start` / `candidate` / `admit` / `download` / `hop_end` / `stop`），每条带 `source`、`score`、`reason`、`hop`；进度与告警走 stderr。`--format md` 把同样内容渲染成给人看的表，仍走 stdout，事件不重复打。`new` 打印 slug 供下一条命令使用；`status` 默认 JSON；`stop` 对已停的项目退出 0 并说明。只有 `run` 收尾时 `corpus/` 为空才退 1。

浏览器路径在测试里用假会话打桩（照 cnki/douban 的做法），不需要真 Chrome；只有 `browser login` / `browser status` 的活体用例允许 skip。`channel="chrome"` 找不到真实 Chrome 时退回 Playwright 自带 Chromium 并告警（清关可能更难过）。`[network].proxy` 必须同时作为 `--proxy-server` 塞进浏览器启动参数，否则浏览器从另一个出口 IP 出去，清关凭据全废。

## 仓库惯例的落点

- 新增落盘路径只写在 `paths.py`（`project_root()` / `browser_profile_dir()`），命令里不手拼。
- 打分、身份判定、事件序列化、`project.toml` 读写都做成纯函数放领域层（可离线单测）；`commands/survey.py` 只做编排，事件渲染借 `render.py` 的既有形状。
- 所有 `.py` 文案用英文（注释、docstring、argparse help、异常与事件文本）；`README.md` / `README.en.md` / `skills/litcli/SKILL.md` / `config.example.toml` 四处同一提交一起改，`tests/test_docs.py` 与 `tests/test_skill_commands.py` 会钉住。
- `survey` 是要拿到确定结果的命令，按 `fetch` 那档处理：单个来源失败明说，不并入检索的静默扇出。
- 后端注册走 `config._registry()` 的 try-import；`browser.py` 与 survey 相关的可选依赖缺失时要有明确提示，不拖垮其它命令。

## 留给实现者

- `referenced_works` 在 OpenAlex 的覆盖率缺口怎么补（退到共刊/作者/主题标签）。
- 登录态探测的判定细节（URL 白名单、`CPE00001` 与 `/login.jsp` 两个反面特征）。
- snap 的 chromium 与系统 Chrome 的选择次序。
- 长跑的资源上限（同时打开的页面数、profile 损坏后的重建）。
