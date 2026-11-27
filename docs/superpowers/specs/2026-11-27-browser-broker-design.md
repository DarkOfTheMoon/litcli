# 浏览器所有者（broker）设计

## 意图与为什么

一个 profile 上只能有一只 Chrome：第二只拿不到 profile 锁，启动即退。现在的代码把这条约束落成了
“每次启动都清场”——`Session.start` 扫 `/proc`、认一只、把其余的杀掉，谁先看见谁收。并行的命令
（两条 `survey run`、一条 `survey run` 加一条 `fetch`）于是互相杀伤：被收的一方在飞的下载白跑、
凭据续签断掉，只能靠 2026-11-27 的自愈补丁重新站起来。自愈解决的是“崩”，不是“白跑”。

根因是所有权散在每条命令里：发现文件、`/proc` 扫描、清场规则、僵尸 pid 判断，每个进程各揣一份。
本设计把这部分交出去——一个常驻的、按 profile 唯一的所有者（broker）持有 Chrome 与它的生命周期，
客户端只做“连上、开一张自己的页、用、放开”。所有权变成显式的，清场规则只写一遍。

## 目标

1. 并行的命令**不再互相杀掉浏览器**：第一个需要它的人把它拉起，其后所有人接同一只。
2. 每个客户端**自有标签页**：共享一只浏览器时不会互相导航到对方的页。
3. 退役只在**没人用**时发生：最后一个租约释放后按空闲时长退出（默认 10 分钟），不是谁走谁杀。
4. 崩溃可恢复：broker 死掉时客户端能自行重拉一次；客户端被 SIGKILL 时租约由内核释放，broker 不等人。
5. 把上述规则收进一个模块，`Session` 只留客户端语义。
6. **默认就用用户自己的 profile**：已经在他们浏览器里的登录态就是会话，不要求重登；他们那只浏览器
   开着并且开了调试口时**直接接上**，不为一条命令再拉一只。专用 profile 降为可选项（`profile = "dedicated"`）。

## 非目标

- 不做通用 RPC 或代理：**数据仍然走 CDP**，由客户端自己的 Playwright 直连 broker 公布的端口。
- 不支持多用户 / 远程共享（仍是本机一个 profile）。
- 不按站点分 profile，不改登录模型（`browser login` 仍是可见窗口里人工登录一次）。
- 不改 `[browser] enabled` 的开关语义与 cookie 兜底路径。
- 不做 cookie 库拷贝、也不做 `storage_state` 交接：Chrome 的 `Cookies` 是加密库、会话 cookie 还不在库里，
  拷一份只能拿到半份且跟着 Chrome 版本走；接进程或共用目录更短。
- **不替用户关浏览器**：他们开着的那只一律不杀（`stop`、空闲退出都一样）；我们自己拉起的有头实例
  `stop` 也不杀，留给他们关。只杀我们自己拉起、且无头的（看不见的东西留着才是垃圾）。

## 反目标（做成了也算失败）

- 引入一个需要人工守护的进程（崩溃后要用户手动重启即算失败）。
- 客户端在正常路径上仍要扫 `/proc` 或判断谁是僵尸。
- 让 `fetch` 这类短命令为了连一次浏览器而付出超过 1 秒的固定延迟。

## 约束

- 平台：POSIX 与 Windows 都在本次改动内（Windows：`DETACHED_PROCESS` 启动、`msvcrt.locking` 取锁、无 `/proc`
  时以控制口探活为准），Windows 上的验证在本机不可得，按“已写、待核”交付并进验收清单。
  Windows 上是否也有 `SingletonLock` 待核；即便没有，拉起后子进程**立刻以退出码 0 结束**本身就是“profile
  被占、转发走了”的签名，同样按“没开调试口”那句话报。
- 没有 Chrome 时仍要能跑：broker 用 Playwright 的 `executable_path` 拉起随包 Chromium，同样以
  `--remote-debugging-port` 分离启动；没有 Playwright 才退回 cookie 路径。
- `[browser].enabled` 关闭时零改动：不连、不拉、不留进程。
- 版本：broker 与 CLI 同版本才好说话；版本不符按“无人用则替换、有人用在用并提示”处理。
- 安全：控制口与 CDP 口都只绑 `127.0.0.1`；发现文件 `0600`。
- profile 路径：`[browser].profile` 为空或写 `user` 时解析成平台的用户默认 profile
  （Linux `~/.config/google-chrome`、Windows `%LOCALAPPDATA%\Google\Chrome\User Data`、macOS
  `~/Library/Application Support/Google/Chrome`）；写 `dedicated` 时用本工具的专用目录（原来那个，
  即 `paths.browser_profile_dir()`）；其它值按路径展开 `~`。子 profile 走 `[browser].profile_directory`
  （默认 `Default`，只在显式设置时传 `--profile-directory`；用户的活跃 profile 可能不是 `Default`）。
  发现文件与 `browser status` 记、印解析后的绝对路径、`profile_directory` 与来源（`config` / `default`）。
- 迁移：升级后 `[browser].profile` 未设置的安装会改指用户的 profile，专用目录里原有的登录态随之闲置。
  判定条件是“未设该键 + 专用目录里有非空 `Default/Cookies`”，成立就 `notice` 一行指路
  （设 `profile = "dedicated"` 沿用旧目录，或直接接受新默认）。不去自动搬 cookie。
- `[browser].headless` 与用户 profile 的相互作用：留 `false`（默认）。设成 `true` 时，用户自己点图标
  启动的 Chrome 会转发进我们那只隐藏实例——窗口不出现，看起来像没启动；文档要写明。

## 方案

选定 C（常驻 broker）而不是 A（共享 + 短锁 + 租约，规则仍散在客户端）或 B（每进程独立 profile、
storage_state 交接）：A 把问题管得更好，C 让问题不存在；B 牺牲共享凭据且每条命令一只 Chrome。

broker 是**监督者**而不是代理：它负责拉起、持有、公布、空闲退出，数据面仍由客户端用 Playwright
`connect_over_cdp` 直连。这保住了现有 `Session.page` / `download` 的全部实现。

## 形态

新模块 `litcli/browser_broker.py`：

- `Broker(profile, headless, launcher)`：`serve()` 绑定控制口、拉起浏览器、写发现文件、进事件循环；
  `launcher` 可注入以便离线测试。
- `main(argv)`：`python -m litcli.browser_broker --profile P [--profile-directory D] [--headless]
  [--channel C] [--proxy U]`，由客户端分离启动（POSIX `start_new_session=True`，Windows `DETACHED_PROCESS`）。
  `--profile-directory`、`--channel` 与 `--proxy` 由客户端从配置算出传进来，broker 不自己读配置。
- 锁：`<profile>/../<profile 名>.lock` 的 `LOCK_EX`（POSIX `flock`、Windows `msvcrt.locking`），**按配置里
  实际使用的 profile 取**，只包住“连或拉”这一段，拿到后重读发现文件。`terminate` 判定“是否最后一个租约”
  也在同一把锁里做——否则新客户端还没发出 `hello` 时，离开的一方会把 broker 关在它脚下。
- 空闲：`leases == 0` 且自最后一次释放超过 `[browser].idle_minutes`（默认 10，`0` 表示不按空闲退）即自杀；
  `login` 拉起的 broker 记 `pinned = true`，不按空闲退出，直到 `browser stop`。
- `pinned` 的陈旧（崩溃 / 重启）：不做清理器，靠 `pid` 死或控制口不应答判陈旧，下一个客户端直接替换。

### 控制协议

控制口上一行一条 JSON，一个连接一条命令；`hello` 的连接在客户端存活期间**保持打开**，即租约，EOF 即释放：

| 命令 | 应答 |
|---|---|
| `{"cmd":"hello","version":V,"headless":B,"pid":P}` | `{"cdp_port":N,"broker_pid":P,"headless":B,"pinned":B,"owns":B}`，连接转为租约 |
| `{"cmd":"status"}` | `{"cdp_port":N,"pid":P,"headless":B,"leases":K,"idle_s":S,"version":V,"profile":S,"owns":B}` |
| `{"cmd":"shutdown","force":B}` | `{"ok":true}`，无租约或 `force` 时退出 |

协议错误回一行 `{"error":"..."}` 并关闭该连接，不影响其它客户端。

### 发现文件

`paths.browser_session_file()` 由 broker 独占写：
`{"pid","control_port","cdp_port","profile","profile_directory","owns","version","headless","pinned","started","leases"}`。
只有 broker 写；客户端只读。文件在浏览器可达之后才落，落盘前客户端视为“还没有 broker”。

### 客户端（`litcli/browser.py` 的 `Session`）

- `ensure()`：读文件 → `hello`；无文件/连接失败 → 抢锁 → 重读 → 仍无则按三态表接、报、或分离启动 broker
  并等文件（上限 20 秒）。版本或 `headless` 不符：无租约则 `shutdown` 后重拉；有租约则沿用并 `notice` 一次。
- `start()`：`ensure()` + `_attach_engine(cdp_port)`，保留一条引擎。
- `_page()`：**每会话一张页**——一律 `context.new_page()`，记住自己的那张，之后只用自己的；
  `pages[0]`（用户或别人已有的页）原样留着、不被导航。
- `reset()`：把自己那张页归 `about:blank`，不碰别人的页。
- `close()`：先把租约连接关掉，再关自己的页与引擎——租约先释放，broker 的计数不与页的析构赛跑。
- `terminate()`：`close()`；若本进程是最后一个租约持有者且 broker 不是 `pinned`，在锁里 `shutdown` 掉。
- 数据面失败（`TargetClosedError` 等）：丢掉 runtime、重新 `ensure()`、**重绑 context 与 request context**
  后重试一次（只在记住的那张旧页上重放会再抛同一错），仍败则 `BrowserUnavailable`。
- `alive()` 的对外语义保留（`_entitled._session_live`、`fetch._download_any` 依赖它）：改为“控制连接在”，
  无 broker 时为假；不再有 `/proc` 分支。

### 自愈与崩溃

- broker 死：客户端的控制连接先断 → 下一次 `ensure()` 重新拉起；每次调用最多重试一次。
- 客户端 SIGKILL：控制连接 EOF，broker 释放该租约，空闲计时开始。
- 接在用户浏览器上时他们关掉了窗口：broker 与 CDP 一起消失，那是“用户不要了”，不是崩溃——
  下一条命令按三态表重新判定（他们没开端口就报那句提示，而不是拉起一只顶替他们的）。

### 迁移掉的东西

`_running_on`、`_reap`、`_wait_gone`、`_is_headless`、`_free_port`、`_launch_detached` 从 `Session`
消失，进 broker；`Session` 不再有 `alive()` 的 `/proc` 分支（保留给 `_entitled.has_session` 的同名语义，
改为“控制连接在”）。

`_reap` 的适用面收窄，判据从“在这个 profile 上”换成“看是谁的”：命令行里带 `--user-data-dir=<profile>`
**且带 `--headless`** 的进程无歧义是我们的（人的桌面 Chrome 不开无头），一律回收，broker 崩溃留下的
隐藏实例因此不会越积越多。有头的先认作别人的：在用户 profile（或人手写的路径）上交给三态表报错；
只有 `dedicated` 目录里照旧清场，那里没有别人的东西。

### 复用用户自己的浏览器

判定按下面这张表走，**每条命令都先判**（锁里重判同一张表，见“最可能做错的几步”第 1 条）：

| 状态 | 判定依据 | 行为 |
|---|---|---|
| 用户 Chrome 在跑，且开了调试口 | profile 目录下 `DevToolsActivePort` 可读，**并且**它给的端口上 `/json/version` 在 2 秒内应答 | 接上。不拉新浏览器，`owns = false`，不设 `pinned`；空闲退出只断连，**不杀** |
| 用户 Chrome 在跑，没开调试口 | 上面不成立，而 profile 的 `SingletonLock` 指着一个活进程（或拉起后子进程立刻退 0） | 明说并退 1：`your Chrome is already running on this profile without a debugging port - close it, or start it with --remote-debugging-port=0`（**不杀**，见下） |
| profile 空闲 | 两条都不成立 | 正常拉起，`owns = true` |

`DevToolsActivePort` 可能是上一次带端口运行留下的陈旧文件，所以只认“文件 + 端口应答”两条同时成立；
只看文件会把“在跑但没开端口”误判成可接，然后连一个死端口。

**撞上被占的那只时退 1，不自动退回专用 profile。** 理由是退回后拿不到登录态，而需要浏览器的场合恰恰
就是订阅站——静默降级会变成“能跑但只能看摘要”，比一句能照做的提示更难查。想常驻走专用 profile 的，
设 `profile = "dedicated"` 即可；给用户的解法则只有两条：关掉 Chrome，或带 `--remote-debugging-port=0`
重启它。

**新建**：机器上从来没有过那个 profile 时（从没启动过 Chrome），第一次运行会把它建出来，等于一份新的空
身份——要空白身份就用 `dedicated`，别让默认值替人决定。

接到用户那只浏览器上时另有三条：

- **绝不导航 `pages[0]`**：那是他们自己的标签页。会话一律 `new_page()` 自己的，`reset()` 也只归自己那张。
- 他们关窗口即会话结束：接下来的调用走上面的表重新判定，不当成 broker 崩溃去“自愈”。
- 我们的写（清关 cookie 刷新、`sciencedirect` 的 `pdfft` 会话令）落在他们真实会话上——这是复用的代价，
  不是缺陷。扩展与同步照常加载，首启比专用 profile 慢，扩展也可能干扰页面；他们 Chrome 关着时由我们拉起，
  Chrome 会顺带恢复他们上次的标签页，窗口不是干净的。

**预热**：挑战是跟着“已登录这条链”来的，不是跟 IP 或 profile 本体。实测同一篇 ScienceDirect 文章：
空 profile 一次 200；用户的 profile 直开是 Turnstile（`Just a moment...`、`Are you a robot?`）；先开站点
首页再开文章才 200；把该站点的 cookie 全删掉又变回 200。所以 `Session.page` 的挑战分支补一步：等待后仍是
挑战页、且本次会话挂在**用户的 profile** 上时，先导航站点根、读完，再重试原地址一次；仍挑战则照旧把挑战页
交给调用方。机构权限（`pdfft` 锚点是否到手）不在这条保证里，见验收第 15 条。
本节的实测记录（三态判定、粘住的 Turnstile、删 cookie 的对照）在
`/home/zhanghuijie/.claude/jobs/de084285/tmp/reuse/FINDINGS.md`（job 临时目录，删 job 后失效）。

### 命令面

- `browser status`：不再在结束时杀它拉起的浏览器，只 `close()`；输出解析后的 `profile`、
  `profile_directory`、`mode`（由 `owns` 推出：`attached` / `launched`）与 `leases`。
- `browser stop`（新）：`shutdown force`，无 broker 时报“没有在跑的会话”并退 0。谁被关只按 `owns` 与
  `headless` 决定，没有 `--force` 的分叉：`owns = false`（接的他们的）留着，输出 “your Chrome was left
  running”；`owns = true` 且有头的留着，输出我们拉起的那只的 pid 与“close the window to end it”；
  `owns = true` 且无头的关掉。空闲退出按同一张去向表。
- `browser login`：要窗口。现有 broker 无头且有租约时，先等租约清零，最多 30 秒；等不到就提示
  “有人在用，稍后再 login”并退 2，不杀在飞的活。等到了或本来没人用，则替换成 `pinned` 的可见 broker；
  命令结束只断开。复用用户的 profile 时登录态本来就在他们浏览器里，`login` 只负责把 broker 拉起来
  并把站点预热一遍，不要求他们重登；已经是 `attached` 的（他们的窗口就开着）没有可替换的，只预热；
  是我们拉起的可见实例的，只补记 `pinned`，不重启窗口。

## 验收

单变量、可复跑，脚本落在证据目录：

每条都要断言**可观察的事实**，不能只看返回值：

1. 两进程并发 `download` 同一 broker：两条都成功；且从 broker 侧 `context.pages` 读到的是两张不同的页、
   两边的页 URL 各自停在各自的地址（只比返回的 `PageResponse.url` 会放过“仍抢 `pages[0]`”的实现）。
2. 客户端 A 下载中 SIGKILL 客户端 B：A 的下载拿到完整字节（现在会被杀）。
3. SIGKILL broker 后，客户端下一次调用自愈成功；**并且**正常复用路径上租约计数确实在涨——只保留旧的 pid/port
   探针会让这条在“从没发过 `hello`”的实现上也是绿的。
4. 无租约后 `idle_minutes` 到点 broker 退出；`pinned` 的不退；`idle_minutes = 0` 不退。
5. 版本不符：无人用则替换；有人用则沿用并只提示一次，**且替换时要证明在飞的那条副本没被截断**——新 broker
   可达但旧 broker 是在客户端用到一半时被关的，一样算不通过。
6. 现有三条探针（裸杀 / 同伴真活 / 同伴假活）全绿；`[browser] enabled = false` 时零连接、零进程。
7. 没有 Chrome 的机器上（`PATH` 里藏起三个 Chrome 名）broker 走随包 Chromium，取页照常。
8. `login` 撞上有租约的无头 broker：租约在 30 秒内清零则换成有头窗口；否则退 2 且原 broker 与使用者的副本完好。
9. Windows：`DETACHED_PROCESS` 启动与 `msvcrt` 锁有对应单测；真机验证标**待核**并列进报告。
10. 用户 Chrome 开着（带 `--remote-debugging-port=0`）：命令接上它，**没有**起第二只浏览器
    （浏览器主进程 pid 与 `chrome` 进程总数前后不变），`owns = false`；空闲到点 broker 退出而他们的 pid 仍活。
11. 用户 Chrome 开着但没调试口：命令退 1 并给出那句 `without a debugging port` 提示；**不是**
    今天这种 `start()` 成功、随后 `TargetClosedError`（复现脚本见证据目录里的旧行为记录）。
12. 只有陈旧的 `DevToolsActivePort`（文件在、端口无人听）：仍按“没调试口”处理，不试图去连死端口。
13. 接在用户浏览器上时，命令的导航**没有**动他们已有的标签页：跑前记下 `context.pages[0].url`，跑后不变，
    且会话自己的页是另一张。
14. 用户把 Chrome 关掉（profile 空闲）：下一条命令在**他们那个 profile** 上拉起并成功，cookie 在
    （用截图或 `context.cookies("https://www.sciencedirect.com")` 里有 `SD_REMOTEACCESS` 作证），
    不再报 `Opening in existing browser session`。
15. 已登录态下直开 ScienceDirect 文章（用户 profile）：`Session.page` 走到预热分支，最终拿到 200 且标题是
    文章标题而不是 `Just a moment...`。机构权限本身（HTML 里有 `pdfft` 锚点）本条不断言，
    实测记录，今天的结果是**只到摘要页、无 `pdfft`**，标**待核**。
16. 未设 `[browser].profile` 时解析为用户默认路径，`browser status` 的 `profile` 就是它、来源是 `default`；
    设 `dedicated` 时解析回 `paths.browser_profile_dir()`、来源是 `config`。
17. 迁移提示：造一个“专用目录里 `Default/Cookies` 非空、且没有 `[browser].profile`”的配置，运行时打出那行
    notice 且它指出 `profile = "dedicated"`；设了该键后不再打。
18. `browser stop` 的三种去向各一例，且都查进程：接的（`owns = false`）→ 他们的 pid 仍活；我们拉起的有头
    → pid 仍活且输出里有那个 pid；无头 → pid 消失。空闲退出同样不许杀前两种。
19. 冻结构建的启动形状（`litcli -m litcli.browser_broker`，不带 `python -m`）能起 broker，拉起的浏览器 argv
    带着它拿到的那条 `--user-data-dir`；默认 profile 上无头启动在**`XDG_CONFIG_HOME` 未设**与**已设**两个信封下
    都拿到调试口、都读到已登录 cookie、都不落一次性 `scoped_dir`，且子进程拿到私有的 `XDG_CONFIG_HOME`。
20. 拿到 broker 后把 `[browser].profile` 改到另一个目录：上一条命令自己拉起时先退役旧 broker，新的 profile
    被服务，旧 broker 的进程消失（不许两只并存、不许把新命令接到旧 profile 上）。

## 最可能做错的几步

1. `ensure()` 的“无文件 → 取锁 → 重读 → 拉起”与另一个进程的 `terminate()` 相撞：关停判定不取锁，
   就会在新人发出 `hello` 之前把 broker 关在它脚下。关停与拉起必须共用一把锁。“接、等锁、拉”三步
   都要在同一把锁里重读一次发现文件与那张三态表。
2. 租约释放的先后：先关页/引擎再关 socket，broker 的 EOF 计时会与页析构赛跑，`status` 也会读到中间态。
   顺序固定为“先 socket，后页与引擎”。
3. 自愈重试：只 `ensure()` 而继续用记住的旧页 / 旧 request context，会原地再抛 `TargetClosedError`。
   必须整体重绑。
4. 只判 `DevToolsActivePort` 文件存在就去连（不试端口）：陈旧文件连不上，退化成“重拉”，再撞 profile 锁。
5. 接上用户浏览器之后仍按 `pages[0]` 用：把他们正在看的页面导航走了——用户最先注意到的就是这个。
6. 空闲退出 / `browser stop` 把用户那只浏览器杀了（`owns` 判反，或照搬旧的“谁看见谁清场”），窗口被我们关掉。
7. 默认翻成 user 只在文档里改、代码仍回落到专用目录，或者把 `dedicated` 这个逃生口做丢——升级后的安装
   静默失去登录态，还查不出为什么。
8. `stop` 把 `owns = true` 的有头实例也杀了：用户回来发现窗口自己没了；或无头实例反而不杀，进程越积越多。

## 留给实现的选择

控制口的报文细节、`Broker` 内部结构、测试里如何替身控制连接（`socketpair` 或注入 transport）、
`idle_minutes` 的读取时机（启动时读一次即可）、`DevToolsActivePort` 与 `SingletonLock` 的读取放进哪个
helper（建议与 `cdp_ready` 并列，纯读文件 + 一次 2 秒连试，可离线注入根目录单测）、
预热分支挂在 `Session.page` 还是各出版社后端。
