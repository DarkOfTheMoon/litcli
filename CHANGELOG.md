# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `refs`/`citing`/`related` 三个图谱命令 漏掉整类答复错误：`KeyError`（200 而正文空）、`ValueError`/`TypeError`（200 而结构不同）、
  `AttributeError`（正文是列表而不是对象）此前穿过分类捕获把整条命令掀掉，现在与 404 一样只损这一源并换下一个；
  坏掉的 `--backend` 或 `源/` 前缀转成 `BadInput`（退出码 2）而不是「意外错误」。
- `related --limit 0` 的关键词兜底拿到上限 0，恒返回空表。
- 图谱不再向对该键没有拼写的源发请求：W-id 发给只认 DOI 的对服务、`openalex/W…` 发给 crossref 之类，
  每次只是一个必然 404 加一行 `[skipped]`。
- 准入不再因一个声明轴缺方法就丢整源（消费者按轴问，缺的是那一轴而不是这个源）。
- 调查的额度退还只对最后一行生效（`charged_by` 每行重建），批内某行把额度耗尽的其它行仍记为已展开；
  没有能寻址的源的行（如只有题名、又没有可搜的后端）改为留在候选而非记 `expanded`，不再为它反复翻动。
- 调查在 OpenAlex 未配置时 `deps.backend` 为 `None`，整条 OA 补题录链丢掉：现在回落到图谱源里的同名实例。
- `BotWall` 被当成「不是本行的问题」而不重试，撞墙的行直接作废。
- 逐源展开按轴判失败：某轴全源失败但仍被送去下一跳时不再静默；`NotSupportedError` 也扣一次尝试，现改为退还。
- 水合补题录不看站点是否已用尽，补完还对着耗尽的源发请求。
- Semantic Scholar 的键归一：接受 `https://doi.org/` 前缀，自排除时用解码后的 DOI 比较（编码值与原值永远不等）。
- crossref 的键带 `doi:`/`DOI:`/`https://doi.org/`/`crossref:` 前缀时不剥直接拼 URL。
- Europe PMC 的 PMC 号正则未锚尾（`PMC123abc` 也当成 PMC 号），且 `target_for` 没有 europepmc 分支。
- `_probe_fulltext` 在「没有可用全文」时返回整数 `2`，被当成功。
- 无 DOI 的页面（PDF 落地页）被 `_gated` 的题名门判掉，整条链少一个合法来源。
- `fetch` 对出版社宿主机一律不走朴素下载，哪怕命中的就是直连 URL（如预签名的资源地址）。
- 会话标签页复用上一个条目的地址：导航无声死掉时 `page.url` 仍是上一篇的 PDF，`download` 当场把它当成答复、
  把上一篇的字节存成本篇；`page` 也把上一篇的地址当成答复地址。现在只有导航真的答过（有响应，或地址已变）
  才认标签页里的地址。
- `--scihub` 在拿到落地页后从不执行：链尾 `unpaywall_landing` 排在 scihub 之前，而链一命中即停，
  再问一遍落地页必然得到同一个 HTML。`--scihub` 的典型用途（bronze 记录的落点就是出版社落地页）因此全程
  拿不到副本。`tail` 现在在「已有落地页」的续链里把 scihub 放到最前。
- `_from_scihub` 把失败的全部原因静默吞掉，未命中／被墙／403 与「压根没跑」在输出上同形；现在原因照常上抛，
  落成 `[skipped] scihub: <原因>`。
- 出版社后端的 `retries` / `retry_after_cap`：crossref 与 Europe PMC 的构造函数完全不收，配了这两个键就整源
  建不起来；订阅出版社收下却接不到传输层，配置对它们静默失效。现在两者都显式转给基类。
- OpenAlex 因 key 被拒时只说一句 `HTTP 403: Forbidden`，与机器人墙同形：待激活、已吊销、超预算的
  403 正文里本来带着 `code` 与处理办法，现在一并带进消息（状态与分类不变，仍是 403）。
- 文档：`README.md`/`README.en.md`/`config.example.toml` 对 Semantic Scholar 与 OpenCitations 是否进
  `search` 全量扇出说法互相打架（这两段写出来就会进，内置免 key 语料并不含它们）；`SKILL.md` 的 `refs`
  备选少了 europepmc；`AGENTS.md` 的后端层没有图谱契约（`GraphSource`/`GRAPH_AXES`/`missing_axes`）。

- 调查开局的种子取法：原来按注册表顺序逐个源问，某个源先凑够三篇就停止，三篇原样收下、不排序也不去重
  ——整轮走查跟着那个源的排序走（实测「reduced-order models for turbulent combustion」被 arXiv 带回的一篇
  lattice-Boltzmann 论文带偏，OpenAlex 等其余源一次都没问）。现在问遍所有源，按 `search` 用的同一套相关度
  合并排序、按身份去重后取前三，同一问题得到的三篇种子全部直指该题。

### Known gaps

- 调查的状态面保留了 `arxiv` 一份额度，但没有代码扣它：arXiv 后端不参与展开，入口只作全文来源。
  它出现在 `remaining` 里却从不减少，接上要改整条全文链的记账面。

### Changed

- 调查的准入不再看**总分**，改看**证据**。原来阈值 2.5 恰好等于 `b`+`a`+`d` 三个与课题无关的轴的
  满分之和（1.0 + 0.5 + 1.0），最近一年、被引不错的正常文章无论写的是什么都正好压线；簇一稀疏 `a` 抬到
  1.5、地板变成 3.5，反而高过阈值，恰在最该收紧的停顿期全部放行。现在准入只读证据 `0..1`，按引文图谱
  文献给出的强弱次序取三信号：**集内被引**（集子里有多少篇引用了它，各 `1/3`，饱和 1）、**耦合**
  （与集子共享的参考文献占候选**自身**文献量的比例，分母不低于 10）、**题目**（取最弱必需腿）。地板
  `--floor`（原 `--threshold`）默认 0.2——两条共享参考文献、一条集内引用，或长句题的两半各沾一半；
  `project.toml` 里存 `floor`，旧项目里 2.5 量级的旧值不沿用。`b`/`a`/`d` 退为排序轴。
- 耦合按候选**自身**参考文献数归一（`shared / max(10, len(refs))`），不再按固定 10 条计数：同样是 10 条
  共享参考文献，在 640 条的综述里是一成、在 43 条的专题论文里是近四分之一，不归一就只能选出「大综述」
  而不是「同一题」。这是 BM25 的 `b`（文献长度归一）在引用图上的对应物。
- 题目轴按**腿**判（`minimum_should_match` 的条件式，取自 Elasticsearch）：先把问句在连接词（`for`/
  `in`/`of`…）处拆成腿，两腿以内全取、三腿取二、四腿以上取 75%（向下取整、至少一条）——三腿全取会把
  走查饿死（实测那条题 0 准入）：文献一次只答得上三腿里的两腿，从没有一条题名把三腿全占。证据取**最弱
  必需腿**的覆盖，一个强半边买不了另一个缺半边，题的一腿全空就是 0。打分轴 `q` 仍按全部词的均值平方，
  两条都已过闸的候选靠它分先后：闸管「算不算」，分只负责在算的里面排。
- 匹配改为按**词干**比（Porter 第 1 步里还值得留的几条后缀规则），并允许四字以上的词互相包含：题名与
  问句在单复数上永远不一致（`instabilities`/`instability`、`flows`/`flow`），按字面比会把问句里恰好是
  复数的那一半整片丢掉；包含则让 `nanofluidic` 认下 `fluidic`、`viscoelastic` 认下单独写的 `elastic`。
- 走查维护一份集内被引计数（由已准入行的参考文献累加，跳首冻结成快照，与跳首集子同一份）：一条集内引用
  比一条共享参考文献更近，是一条直接边而不是共被引。
- 单跳准入数不再先到先得：档位准入数按**证据**从高到低截取（证据相同才看总分，再相同按到达顺序），
  被截掉的记 `rank k of n for cap c`；跳内所有候选对同一份跳首集子打分。低证据而高分（近作、高被引、
  研究类型）的一篇，不再挤掉真正答得上题的那篇。
- 证据地板定在「一个词」与「两个词」之间，并有不变量用例钉住（`test_the_floor_sits_above_one_word_and_below_two`）：
  地板低于单词分就放任何人进来，高于两词分则首跳（还没有簇，只能靠题目轴）一篇都收不进。两条共享参考
  文献与一条集内引用同样够过线（各自 0.2、0.33），所以地板抬高没有卡死首跳之后的任何一条通道。
- 题目轴的覆盖按**平方**计、并滤掉停用词（`of`/`in`/`the` 这类词几乎出现在每篇题名里，留着就是给任何记录
  白送分；词表不足两个时按两个算，单词语题不会被一个命中答满）。线性覆盖只要沾上一个词就算数，实测长句题
  「transient growth of viscoelastic instabilities…」四跳收下 8 篇全是微流控通论、`viscoelastic` 一个都没进；
  平方之后「覆盖半句才算有证据」由地板自动成立。
- 打分多一根**题目轴**（`q`，权重 2.0，在共被引之下、被引与新近之上）：候选题名/摘要携带多少课题词。
  `c` 只说明「像已收下的东西」，集子一起漂就拉着自己的邻居一起漂，`a` 又恰在走查停顿时提权三倍、把自己
  送进「本领域的正典」，两条合起来就是实测里 1.5 档末尾漂到 CFD 经典文献的成因。`q` 不跟着集子漂，也是
  首跳与只有题名的记录（crossref 参考行，原来恒 0.7 分、永远够不到准入）唯一能用的轴。共被引饱和点同时从
  20 条降到 10 条——有题目轴兜底后，这个轴不该长期贴地板跑。
- 引文图谱的 `refs`/`related` 改为多源合并：首个答上的源定表序，其余能答的源接着补、按身份去重后补到
  `--limit`，新增行只在自身之间按被引降序、无计数者置底，`--sort cited` 才对合并后的整表排序；`citing`
  仍只取第一个答案（引文索引之间重叠太高），`--backend` 与 `源/` 前缀钉死一个源、不再合并。
- 调查每跳的轴额度不再先到先得：按「声明该轴且能寻址」的源拆份发放（商向上取整、短答把结余交给下一个、
  每个源还有自身上限），一份长参考文献不会再独占整份额度，重复身份不占份额。
- 档位的站点额度按该档自己的步子重定：一次展开实测约 10 个请求（`references` 是一次调用外加每个被引
  条目一次），原值只够两跳就用尽，`low` 走查在第 4 跳撞到 `budget` 而不是走完自己的 6 跳。`graph_cap`
  现按 `admit × max_hops × 10` 取值（low 120 / medium 1000 / high 4000 / max 14400），并在 `effort.py`
  注明它是预扣量而非硬上限——单个宽答案仍可能把站点带出去一两个请求，计数器照实记（可为负）。

## [0.5.0] - 2026-11-25

### Added

- 引文图谱多了四个应答来源：新增 Semantic Scholar（三轴齐备，推荐即语义邻居）与 OpenCitations COCI
  （CC0 的引用/被引 DOI 对、无题录）；crossref 补 `references`（沉积的参考文献表，仅反向）、Europe PMC 补双向；
  OpenAlex 补声明它本就答的三轴（此前只声明 `read`，被能力门当成无轴源，一条边都走不到）。
- 逐源降级：`refs`/`citing`/`related` 与调查的每一跳不再只问 OpenAlex。源按能力轴选择，按
  openalex → semanticscholar → opencitations → crossref → europepmc 逐个问，被限流、额度耗尽或查不到就
  `[skipped] <源>: <原因>` 换下一个，只有全都答不上才抛最后一个原因（墙压过 404：「源已用尽」比「这个 DOI
  服务没有该记录」更能解释空结果）；一方答「没有」也让下一个说，调查里报错的一行保住已交回的候选、不记作
  已展开。只有链接没有题录的源，行内裸 DOI 先用能读题录的源补齐，打印出的标识符可以再问一次；跨源展开各说各的
  标识符、按聚合身份去重，每跳的轴内额度跨源共用。
- 图谱契约 `GraphSource`：声明了某轴却没有对应方法的源在准入时就被挡下，不会等到跳内炸成 `AttributeError`；
  一个源的墙只损失它自己的回答。

### Fixed

- Europe PMC 的参考文献与前向取错了容器：EBI 把两者分别放在 `referenceList/reference` 与 `citationList/citation`，
  此前按同名字段读，`references` 恒为空。
- 图谱与调查对单条失败的捕获过窄，`AttributeError` 会穿过分类异常把整轮掀掉。

## [0.4.1] - 2026-11-25

### Fixed

- 会话里取 PDF 的等待原来写死 4 秒：闸后站点先回挑战页、解完才变下载，4 秒一到就转兜底，
  拿到的是挑战页 HTML 不是 PDF。现在按页面判而不是按时钟判——还是挑战页就等（上限由 4 秒改为五分钟），
  一旦是文档（内联查看器、被送回 `/abs/` 的无权页、登录页）就当场停并交回该页，由调用方分「闸」与「无权」；
  传输落地不限时地等，导航与兜底请求各给五分钟，用完还原，不影响会话的其它等待。
  导航被整个中止（`net::ERR_ABORTED`，doi.org 直接给文件时就这样）也按「页面即答复」处理，不再当成崩溃；
  导航一直不答（接了请求就不回包的句柄）也不直接报错，而是再走一次直接请求。
- 调查里一个接了请求却不回答的链接不再整轮崩掉：它抛的是 Playwright 自己的 `TimeoutError`，不是
  `OSError`，此前一路穿过分类捕获，在首跳的种子上就把整轮掀了。现在只损这一份、记成 `error:<类型>`，
  种子里同类的超时也一并收进受控范围。

### Added

- 课题调查 `litcli survey new|run|status|stop`：一个课题是一个目录（项目配置、候选理由、逐步 trace、
  corpus），`run` 一跳一跳沿引文图谱收网、每跳落检查点，被杀后同一条命令续跑且不重复展开。额度是档位
  （low/medium/high/max）不是条数：边的读取额度按每篇作品算，站点请求上限才是全程预算，按请求计、命中缓存退还；每条候选都有理由，下载失败只标 `unavailable`
  不剔除。同项目单写者由 `$.lock` 保证，停止走 `$.stop` 哨兵。开局检索的种子会先归一到 OpenAlex 认的键
  （图谱只按它展开），能查到的一律落成 work id（种子被引加分按它比对）；某一行展不开时记一条 `error` 事件跳过，不拖垮整轮。收尾的退出码按落下的正文副本算，只有元数据旁车也算一份没落下。
  种子与续跑遗留的已准入行同样会取正文（每行一次，行里的 `download` 记过就不再取）；SIGTERM 与 Ctrl-C 同等对待：第一下结束当前跳并留下可续跑的检查点、第二下才中止，锁不会被留下。

- 共享浏览器会话：`litcli browser login|status` 与 `[browser]` 配置（默认关闭）。机构登录一次后，出版社页与
  PDF 都在活浏览器页内取，凭据不出浏览器、过期自续；浏览器独立于命令行进程运行（`--remote-debugging-port`
  发现文件），后续命令经 CDP 接上同一只，关窗口才结束；关着或会话失效时逐字走既有 cookie 路径；窗口在下载途中被关只损失这一份，自动改走 cookie 路径重试一次，不打断整轮。
  开了 `[browser]` 的调查也会带这只会话：链条给出的开放获取链接常落在只有浏览器过得去的出版社主机上（link.aps.org 之类），纯 HTTP 被闸后依次改用会话、再退到该作品自己的开放获取副本。
  机器人闸的首屏是挑战页，托管挑战十来秒后会自己换页过去，所以判定前先等它过完（换页途中读不出内容视为仍在挑战中），
  等不到就再导航一次、让刚落下的清关 cookie 起作用——被判 `gated` 的是真过不去；导航只等 DOM，不等 `load`（挑战会挂住子资源，等 `load` 的站点会一直超时）。
  `browser status` 对没在时限内应答的站点给 `no answer` 这一档，超时先重探一次，两回才判，异常只占一行。
  浏览器按 profile 复用：有活着的就接上（发现文件丢了也按 `/proc` 把它找回来重写文件），并把同 profile 上多出来的重复实例停掉，
  机器上始终只有一只；本次命令拉起来的那只随命令结束关掉（调查收尾、`browser status` 探完），用户 `login` 开着的那只留着复用；
  登录要有窗口，遇到在跑的是无头浏览器就换掉它（同一 profile，cookie 不丢）。`login` 收尾先断开再退出，
  不会在shell 提示回来后冒出 Playwright 驱动的 EPIPE 堆栈；结束靠终端里按回车，提示语已写明。

## [0.4.0] - 2026-11-24

### Added

- 11 家出版社订阅全文阅读器：ScienceDirect、Wiley、SpringerLink、Cambridge Core、AIP Publishing、ACM、APS、
  IOPscience、Taylor & Francis、AIAA、IEEE Xplore。只读、只按 DOI / 文章 URL 解析，`fetch` 仅对各自 DOI 前缀
  自动尝试一次；命中标 `kind` 与 `access=entitled`，被 Cloudflare / WAF 闸拦下时报因并由链尾续接。
  会话按 `cookie` + `user_agent` 成对配置，OA / 金 OA 条目免配置。

- 结果里出现 `[public]` 副本（Sci-Hub / Z-Library 等镜像）时，stderr 打一行说明「不是开放获取授权，许可见标签」，
  整轮只打一次：判断许可的责任落在读者身上这件事，标签本身交代不了。

- 检索表格在标题下补一行缩进摘要（160 字截断加省略号）：光看标题判不出该不该要，摘要片段才够；
  只对有摘要的记录输出，JSON 仍走 `abstract` 全字段。

- 元数据读取的本地缓存：成功的 GET JSON/文本存 24 小时（`~/.cache/litcli/cache`），重复检索不再重复消耗站点配额；
  写操作与文件下载不缓存，条目损坏/目录不可写一律降级为直接请求。`--no-cache`（`LITCLI_NO_CACHE=1`）绕过。
  重复同一检索实测 10.5s → 3.6s。
- `search --all` 改成最多 4 路并发，按后端顺序归并，结果顺序仍确定；整轮设 45 秒上限，
  到点没答的来源按超时计入失败并按静默策略处理，一个卡住的重试不再把命令拖成两分钟。


- 来源性质标注：每个后端声明自己给的是什么副本（`oa` 开放获取授权 / `public` 公开副本、非 OA 授权 /
  `local` 你的库），表格行内标 `[oa]`/`[public]`/`[local]`，JSON 给同名的 `access` 字段，`fetch` 的
  命中与候选列表也一并说明。标签跟着记录走，后端改名或换实现不影响。
- `--debug`（或环境变量 `LITCLI_DEBUG=1`）：把每个请求、状态码、耗时、限速等待与重试逐行打到
  stderr，query 里的 key / email 打码；意外异常额外打印调用栈。该开关在子命令前后都接受，
  stdout 不受影响，不开启时只有一次布尔判断的开销。
- `--verbose`（或环境变量 `LITCLI_VERBOSE=1`）：检索里哪个来源、什么原因失败逐条写 stderr，默认静默；
  `search` 全部来源都失败时即使不加也会打印一行并退 2（那不算「没有结果」）。
- key 自带来源前缀即可路由（`litcli data get openalex/W123`、`zotero/AB12`），显式 `--backend` 仍然优先；
  DOI 里的斜杠不会被误认作前缀。
- 本机 Zotero 没开时，检索与写库先探一次端口直接报错并给出启动提示（含 `--backend <source>` 的去路），
  不再等浏览器式超时。

### Changed

- 检索结果默认按**相关度**排序（标题命中重于摘要、命中全部词重于命中一个，同分再看被引与年份），
  `--limit` 截到的因而是最贴题的几条，而不是哪个后端先答完；`--sort cited` 仍是纯被引排序。
- `--title/--author/--year/--container` 除了照旧映射给后端，还会在本地再判一次：后端忽略某个条件
  （或只把它折进自由词）时，记录不会再漏过去。
- `--all` 去重身份从「归一标题」收紧为「归一标题 + 第一作者」，且年份相差超过 1 年不合并：同名的
  `Editorial` 不再被捏成一条，同一篇预印本与刊出本仍能合并；反过来，**带 DOI 的记录会吸并同标题同作者的
  无 DOI 记录**，不再留成两行。
- 各来源的引用数互相打架时保留最大值供排序，但把每个来源各自的数记进 `extra["cited_by_source"]`，
  表格改为 `[cited N via 源]`，来源不一致时追加 `(sources disagree)`，不再把单源数字当成共识。
- 后端失败改为带分类的异常（`litcli/errors.py`）：`bad-input` / `not-found` / `paywalled` / `auth` /
  `bot-wall` / `unavailable` / `unsupported` / `external`。调用方（`--all` 聚合、全文链、CLI 出口）
  按分类决定策略与退出标签，不再靠字符串匹配或笼统的 `RuntimeError`；HTTP 状态经 `http_kind()`
  统一归入同一套分类，各后端只在站点含义不同处就地上报。
- 出错时可附一行 `[hint]`（如缺浏览器时的 `litcli backends install-browser`、Zotero 未启动时的启动提示），与错误正文分开呈现。
- 失败呈现分层：`search` 扇出默认静默（详见上），`fetch`/`data`/`export`/`refs`/`citing`/`related`/`backends`
  这类要拿到确定结果的命令失败一律明说。
- 出版社阅读器收口到同一基类与一张站点表：`resolve_fulltext()` 的具名参数
  `sciencedirect_backend=` / `wiley_backend=` / `springer_backend=` / `cambridge_backend=` / `aip_backend=`
  并入唯一的 `publishers=` 字典。仓外按旧签名传参的调用方会得到 `TypeError`，请改用 `publishers`。

### Fixed

- Windows 上 `tests/test_cache.py` 的剪枝用例偶发红：它在同一循环里连写五条，Windows 的时钟粒度（15.6 ms）
  让它们的 `mtime` 全相同，"最旧的先删"退化成目录列举顺序。改为逐条打上递增的 `mtime`。
- IEEE Xplore 换了闸：除了 AWS WAF，又多一层 F5 拦截页（`200` + `<APM_DO_NOT_TOUCH>` + `/TSPD/…`
  脚本，正文只有导航与标题、没有 `citation_*`）。此前它当普通页面解析，于是报「未订阅」；现在按 WAF 闸
  认下，报的是「该站要 JS、请用带会话的浏览器」。闸提示与构造期报错同时把 `TS*` cookie 与 `aws-waf-token` 并列。
- Windows 上 `backends authorize` 写回的 api_key 含非 ASCII 字符时回读乱码（按系统默认的 cp1252 解码
  UTF-8 文件）：配置读写与测试回读一律显式 UTF-8，跨 locale 的子进程调用同样命名编码。
- 构建依赖的 `setuptools` 上限从 `<81` 前移到 `<85`：CI 镜像自带的 84 会被 `--no-isolation` 的轮子
  内容测试判为 unmet dependency，连续把 master 打红。
- `fetch fulltext <doi>` / `fetch --download`（0.1 的旧写法，老文档与旧技能副本仍在教）不再报一句裸
  argparse usage 让人以为是 DOI 不被接受：`fulltext` / `book` 被当作旧动词认下，`--download` 作为
  无操作别名保留（下载本就是默认）；顺带未加引号的书名也能直接给。
- `related` 在 OpenAlex 没有 `related_works` 时不再只回「没有」：退回用该文标题里的实词再检索一次，
  剔除种子本身并标 `extra["related_via"]="keyword"`，stderr 说明这是标题匹配（老文献、冷门文献大多没有邻居列表）。
- 无 DOI 的 OpenAlex 记录（预印本、技术报告）key 曾为空，表格行 `openalex/` 无法复制、`get` 也解析不了；
  现在回落到它的 W-id。
- 表格不再截断标题（60 字）与作者（24 字）：标题整句显示，作者超过三人折成 `et al.`，并补出刊名与 DOI——
  截断后的标题没法判断要不要，DOI 正是下一步 `fetch` 的入参；`data members` 同样不再截断。
- `data get arxiv/1806.09180` 打到 DOI 后端时曾 404：现在与 `export` 同样先把 arXiv id 归一为 DOI，
  Zotero 等本机后端仍用自己的条目 key。
- 无终端（管道、脚本）时 `data add --pdf` 的 y/N 复核曾静默读成「取消」：现在明确报错并提示 `--yes`。
- `--backend` 默认值改为在分发处回落，`source/key` 前缀与显式 `--backend` 可同时使用而不再互斥出错。

## [0.3.1] - 2026-11-06

### Added

- 二进制安装器（Linux `.run` 自解压 / Windows 安装 `.exe`）：`packaging/` 下的 PyInstaller 冻结配置 + 两个安装器脚本，`scripts/build_binary.{sh,ps1}` 构建并自验（装、跑、卸）。产物只有字节码与依赖，目标机不需要 Python，也不需要源码。CI 增加 Linux + Windows 构建作业。
- `litcli backends install-browser [chromium|firefox|webkit]`：经随包的 Playwright 驱动下载浏览器，落在 Playwright 自己的缓存里，升级复用、卸载保留。Chromium 不塞进安装包。
- `release.sh --binary`：切版时构建本平台安装器并连同 wheel/sdist 一起上传；`verify_release.sh` 在产物含 Linux 安装器时按消费者视角装一遍、跑一遍、卸一遍。
- `backends install-browser --check`：无头启动一次浏览器并报版本（`--dry-run` 只列出下载目标）；CI 在冻结包上真下载 Chromium 后必跑，Linux 与 Windows 都覆盖。
- 冻结包显式把浏览器钉在用户缓存（`~/.cache/ms-playwright` / `%LOCALAPPDATA%\ms-playwright`）：Playwright 在 PyInstaller 环境下默认 `PLAYWRIGHT_BROWSERS_PATH=0`，浏览器会落进升级即被覆盖的包内目录，此前冻结包的下载位置与启动查找位置不一致。
- `--binary` 发版要求 Linux 与 Windows 安装器齐全：另一个平台由 CI 产物或 `--installer <path>` 提供，缺任一平台即拒绝发布，`--release-only` 也会补取；`verify_release.sh` 同步要求 Windows 安装器在产物与 `SHA256SUMS` 里。

## [0.3.0] - 2026-11-06

### Added

- 支持 Windows：配置与浏览器 profile 落到 `%APPDATA%\litcli`（`XDG_CONFIG_HOME` 在 POSIX 上同样被尊重）；
  CI 增加 `windows-latest` 作业跑全量测试与入口冒烟。

### Changed

- `--out` 现在接受目录（已存在或以分隔符结尾），文件名仍按默认规则生成；此前只能给完整路径。
- 仓库内 Windows 调试入口 `bin\litcli.cmd`（等价于 POSIX 的 `bin/litcli`）。

### Fixed

- `fetch` 的默认文件名在 Windows 上非法：老式 DOI 含 `:` `<` `>`（如
  `10.1002/…16:15<1705::AID-SIM599>…`），此前直接拼进文件名。现统一替换非法字符、去尾部点/空格、
  截断过长主干并避开 `CON`/`NUL` 等保留设备名。
- `pdftotext` 的输出按 UTF-8 解码（此前跟系统代码页，Windows 下中文 PDF 会乱码）。
- 重定向输出在 Windows 上强制 UTF-8，避免中文题录在 cp936 管道里抛 `UnicodeEncodeError`。

## [0.2.0] - 2026-11-05

### Added

- `README.en.md`：英文入口页（是什么 / 装什么 / 怎么跑 / 后端与授权边界），与中文 README 互加语言切换；
  `tests/test_docs.py` 比对两份文档的命令与后端清单，防止一边加、一边忘。
- `.github/ISSUE_TEMPLATE/bug.yml`（中英双语）与仓库 topics。
- `tests/test_cli_surface.py`：遍历 argparse 全树逐个跑 `--help`，并断言每个子命令都有对应的分发处理，
  防住「改了参数表没人发现」。
- `tests/test_skill_commands.py`：`SKILL.md` 的命令表与后端名和 parser、后端注册表比对，防文档漂移。
- `scripts/verify_release.sh <tag>`：按消费者视角验证已发布产物——下载附件、核对 `SHA256SUMS`、装进临时
  venv、跑 `--version` 与一条真实检索；`release.sh --push` 发完自动调用，装不起来就算发布失败。
- GitHub 镜像仓承载 CI（`master` 与 `v*` tag 触发 lint / 测试 / 入口冒烟 / 打包），`release.sh --push`
  同步推所有 remote。
- `scripts/release.sh --release-only`：给已存在的 tag 补建 Release（切版时没有 token 时用），不 bump、
  不提交、不推送；`dist/` 缺失会重建。
- 发布 token 也可放 `~/.config/litcli/gitee.token`（`chmod 600`，不入库），脚本优先读环境变量。

## [0.1.0] - 2026-11-04

### Added

- 领域模型 `AcademicItem`（CSL JSON ⇄ 对象、BibTeX/RIS 导出）与 author-date 引文渲染；`export` 产出的
  `csljson` 可直接喂 `pandoc --citeproc`。
- 可插拔后端层：本机 Zotero 10 本地 API（读/写/附件，运行时授权，免 zotero.org Web API key）、arXiv、
  Crossref、OpenAlex（含 refs/citing/related 引文图谱），以及按需启用的 OpenLibrary、USPTO、CNKI、
  豆瓣、Sci-Hub、Z-Library、DOAJ、Europe PMC、DOAB、Zenodo。
- 跨后端聚合：`search --all` 按 DOI → ISBN → 归一标题去重合并，记录全部来源；`--type/--title/--author/
  --year/--container` 字段级过滤，`--sort cited` 按被引降序。
- 全文定位链：本地库 → Unpaywall/OpenAlex → Europe PMC JATS XML → Zenodo 文件 → Unpaywall 落地页，
  `--scihub` 再兜底镜像并标注非 OA；`fetch book` 走 Z-Library / DOAB 出候选书单。
- `data add --pdf` 从 PDF 首页启发式抽候选，交互确认后写库；写前过查重/半残门。
- shell 补全（bash/zsh/fish）：补子命令、选项、选项取值与本地文件路径，脚本自包含、补全时不起子进程。
- 反爬墙的本地解算（不绕过）：Sci-Hub 的 ALTCHA、DOAB 的 Anubis、Z-Library 的 cookie 工作证明都用
  `hashlib` 现场做题；知网/豆瓣用 Playwright 复用本机登录会话，滑块交人工完成。
- 知网连写中文关键词先分词再以 `*`（知网的 AND）连接，否则整串被当成一个词而 0 命中。
- `scripts/release.sh`：门禁（工作区干净、测试、lint、CHANGELOG、tag 唯一）+ 版本 bump + wheel/sdist +
  可选 Gitee Release。

### Changed

- **`search` 默认搜全部来源**（已配置后端 + 免 key 的 OA 语料），`--backend` 收窄到单个，二者互斥；
  单个后端失败只跳过并记一行 stderr，不再让整条检索失败。`--limit` 改为总量上限，不再是每后端上限。
- **`fetch` 收成一条命令**：`fetch <doi|arXiv id|ISBN>`，去掉 `fulltext`/`book` 两层子命令（书名用 `--book`，
  ISBN 自动识别）。默认直接落盘，stdout 只回保存路径、诊断走 stderr；`--no-download` 只回链接。
  arXiv id 现在会被归一（此前 `fetch fulltext arxiv/…` 一律判无全文）。
- **退出码区分结果**：`fetch` 0 拿到 / 1 没有全文 / 2 错误，调用方不必解析英文输出。
- `--download` 在 `fetch` 上成为默认，旧写法需改为 `--no-download`。

- README 重排为用户视角：先讲能做什么、给一条不需要 Zotero 的 60 秒上手，再是安装 / 命令 / 后端授权边界 /
  限制；架构细节收进 AGENTS.md 并改为链接。修正安装只提 PEP 668、`--bump --push` 未提镜像同步、
  测试段漏记新增用例等过时描述。
- `SKILL.md` 补「一条检索怎么走」工作流（先查用户库 → 外扩 → 收敛 → 顺藤摸瓜 → 落盘 → 落不到就明说），
  命令面去掉写死的本机绝对路径，修掉只读后端清单漏项。
- `config.example.toml` 头部补齐可选后端清单，修正 OpenAlex 说明的错字。

- 网络层统一 `httpx`（连接池 / HTTP/2 / 代理 / 超时）、`tenacity` 退避重试、`pyrate-limiter` 站点节律、
  `lxml` 解析，取代早期手写的 urllib 传输。
- CLI 入口按意图拆为 `litcli/commands/` 各族命令，`cli.py` 只留解析、分发与异常映射。
- 所有文本走英文（代码/注释/CLI 输出），用户文档保持中文。

### Fixed

- Zotero 收藏集合成员罗列（Zotero 忽略 `collection` 参数，改由本地 `collections` 字段归属）。
- Crossref 裸 DOI 与 OpenAlex 完整 URL、两种 ISBN 写法统一归一，避免同一本书去重失败。

