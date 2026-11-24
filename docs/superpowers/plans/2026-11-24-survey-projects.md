# 课题项目与自主收网 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 litcli 用常驻的真实浏览器会话取订阅全文，并在一个带出处的课题项目上自主沿引用图收网。

**Architecture:** 两段。第一段把「搬 cookie」换成「附着到活着的 Chrome、页内取件」，cookie 通路原样留作退路；第二段加项目仓库（`project.toml` + `seen.jsonl` + `trace.jsonl` + `corpus/`）与一个按档位限速、可中断可续跑的收网循环，发现只走 OpenAlex。

**Tech Stack:** Python 3.12、Playwright（`sync_api`，浏览器用本机 Chrome）、httpx、unittest（全离线，stub 传输 / 假会话）。

**Spec:** `docs/superpowers/specs/2026-11-24-survey-projects-design.md`

## Global Constraints

- 所有 `.py` 文案（注释、docstring、`help`、异常、事件文本）用**英文**；`README.md` / `README.en.md` / `skills/litcli/SKILL.md` / `config.example.toml` 四处与文档同一提交一起改，`tests/test_docs.py` 与 `tests/test_skill_commands.py` 钉住一致性。
- 测试全离线：网络走 `tests/http_stub.stub()`，浏览器走 `tests/fake_browser.FakeSession`；不许开 socket、不许起真 Chrome。
- 打分、身份判定、序列化、`project.toml` 读写做成纯函数；`commands/survey.py` 只编排；新增落盘路径只写在 `paths.py`。
- 失败呈现按 `fetch` 那档：单个来源失败明说，不并入检索的静默扇出。退出码 `0` 成功 / `1` `run` 收尾时 `corpus/` 为空 / `2` 出错。
- 元数据缓存照旧 24 小时、只存成功 GET；**命中缓存不扣额度，重试扣**。
- `[network].proxy` 必须同时进浏览器启动参数（`--proxy-server`），否则清关凭据的出口 IP 对不上。
- 额度档位见 Task 7 的表，默认 `medium`。

## Review Focus

- 项目目录被手改坏（空文件、半行 JSONL、`project.toml` 缺键）→ `run` 要指到哪一行哪一键，不崩。
- 同一课题重复 `new` → slug 加序号，不覆盖已有项目。
- 一跳中间 Ctrl-C → 续跑不重复下载、不重复扣额度。
- 循环中途浏览器死掉 → 该次下载降级到 cookie 通路，整轮不中断。
- 缓存命中的候选 → 不计入站点额度。
- 无 DOI 的候选（W-id）→ 打分留档，但不去下载。

---

## Phase 1 — 浏览器会话层（可独立交付：`fetch` 不再手工搬 cookie）

### Task 1: 项目路径与浏览器路径

**Files:**
- Modify: `litcli/paths.py`（`cache_dir()` 之后）
- Test: `tests/test_paths.py`

**Interfaces:**
- Produces: `project_root() -> Path`、`browser_profile_dir() -> Path`、`browser_session_file() -> Path`。前两者走 `$XDG_DATA_HOME/litcli/{projects,browser-profile}`（Windows `%LOCALAPPDATA%\litcli\…`），后者是 `browser_profile_dir().parent / "browser-session.json"`。

- [ ] **Step 1: 写失败用例**

```python
def test_project_root_honours_xdg(self):
    with env(XDG_DATA_HOME="/x"):          # tests/test_paths.py 已有该上下文管理器
        self.assertEqual(paths.project_root(), Path("/x/litcli/projects"))

def test_session_file_sits_beside_the_profile(self):
    with env(XDG_DATA_HOME="/x"):
        self.assertEqual(paths.browser_session_file(),
                         Path("/x/litcli/browser-session.json"))
        self.assertNotEqual(paths.browser_session_file().parent, paths.browser_profile_dir())
```

- [ ] **Step 2: 跑用例确认失败** — `python3 -m unittest tests.test_paths -v`，期望 `AttributeError: project_root`。
- [ ] **Step 3: 实现三个函数**，照 `cache_dir()` 的平台分支写（`mkdir` 交给调用方，纯函数不建目录）。
- [ ] **Step 4: 跑用例确认通过** — 同上命令，期望全绿。
- [ ] **Step 5: 提交** — `git commit -m "feat(paths): add project and browser profile locations"`

### Task 2: 会话对象（启动 / 附着 / 取页 / 下载）

**Files:**
- Create: `litcli/browser.py`
- Create: `tests/fake_browser.py`
- Test: `tests/test_browser.py`

**Interfaces:**
- Produces:
  - `class PageResponse: status: int; text: str; url: str; headers: dict[str, str]`
  - `class BrowserUnavailable(Exception)`
  - `class Session:`
    - `__init__(profile: Path | None = None, headless: bool = False, channel: str | None = None, proxy: str | None = None, launcher=None)`
    - `alive(self) -> bool`
    - `start(self) -> None`（已在跑则直接返回）
    - `page(self, url: str) -> PageResponse`
    - `download(self, url: str) -> bytes`
    - `close(self) -> None`
  - `tests/fake_browser.FakeSession`：同样四个方法，`page()` 依次吐排队的 `(status, body)`，`download()` 吐排队的 bytes，并记 `self.urls`。
- `launcher(profile, headless, channel, proxy) -> ContextLike` 是可注入的；默认 launcher 用 `playwright.sync_api` 的 `launch_persistent_context`，`channel` 为 `None` 时不传该参数。

- [ ] **Step 1: 写失败用例**

```python
def test_a_second_start_reuses_the_running_one(self):
    self.s.start(); self.s.start()
    self.assertEqual(self.launcher.calls, 1)
    self.assertTrue(self.s.alive())

def test_start_writes_and_close_removes_the_session_file(self):
    self.s.start()
    self.assertTrue(paths.browser_session_file().exists())
    self.s.close()
    self.assertFalse(paths.browser_session_file().exists())

def test_proxy_reaches_the_launcher(self):
    Session(proxy="http://127.0.0.1:7890", launcher=self.launcher).start()
    self.assertEqual(self.launcher.kwargs["proxy"], "http://127.0.0.1:7890")

def test_a_gate_page_is_returned_not_raised(self):
    self.s.start()
    self.launcher.queue.append((403, "<html>__cf_chl</html>"))
    self.assertEqual(self.s.page("https://x/a").status, 403)
```

- [ ] **Step 2: 跑用例确认失败** — `python3 -m unittest tests.test_browser -v`，期望 `ModuleNotFoundError`。
- [ ] **Step 3: 实现 `browser.py`**。要点：启动走 `launch_persistent_context(profile, headless=…, channel=…, proxy={"server": proxy} if proxy)`；页面用 `context.pages[0]`；`page()` = `p.goto(url)` 后读 `status/content/url/headers`；`download()` 用 `expect_download`；Playwright 未装时抛 `BrowserUnavailable`。
- [ ] **Step 4: 跑用例确认通过** — 同上命令。
- [ ] **Step 5: 提交** — `git commit -m "feat(browser): attachable persistent Chrome session"`

### Task 3: `browser login` / `browser status`

**Files:**
- Create: `litcli/commands/browser.py`
- Modify: `litcli/commands/parser.py:75`（`backends` 之后）、`litcli/cli.py:19`（import）与 `:30`（`_TOP_LEVEL_COMMANDS`）
- Test: `tests/test_browser_cmd.py`

**Interfaces:**
- Consumes: Task 2 的 `Session`。
- Produces: `cmd_browser_login(cfg, args) -> int`、`cmd_browser_status(cfg, args) -> int`。`login` 起会话后打开 `SITE_URLS[args.site]` 并阻塞等回车；`status union list` 逐站报 `signed in` / `signed out` / `gated`。

- [ ] **Step 1: 写失败用例** — 用 `FakeSession` 打进 `litcli.commands.browser.Session`：`status` 对返回登录页的站点打 `signed out`、对 403 打 `gated`、对文章页打 `signed in`；`login` 无 `--site` 时退 2 并提示可选项。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现命令与 parser/dispatch 接线**（argparse help 用英文，`--site` 用 `choices`）。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(browser): login and status commands"`

### Task 4: `_entitled` 走会话，cookie 留作退路

**Files:**
- Modify: `litcli/backends/_entitled.py:51-74`（`__init__`）、`:99-112`（`_open`）
- Modify: `litcli/backends/_gated.py:433-440`（`get_attachment`）
- Test: `tests/test_entitled_session.py`

**Interfaces:**
- Consumes: `Session.page()` / `Session.download()`、`PageResponse`。
- Produces: `EntitledBackend.__init__(..., session=None)`；`self.session`。`_open` 在 `session and session.alive()` 时走 `session.page(url)` 并把 `PageResponse` 直接当响应对象用（`.text` / `.url` / `.status_code` 属性对齐）；`get_attachment` 在同样条件下走 `session.download(url)`，非 `%PDF` 时仍走 `_refusal_reason`。`session` 为 `None` 或已死时逐字走原路径。

- [ ] **Step 1: 写失败用例**

```python
def test_a_live_session_serves_the_page_and_the_pdf(self):
    be = self.make(session=self.session)      # FakeSession: 先 200 文章页，再 b"%PDF-1.4"
    self.assertIn("pdf", be.get("10.1002/x").attachments[0])
    self.assertTrue(be.get_attachment("10.1002/x").startswith(b"%PDF"))
    self.assertEqual(be.transport.requests, [])   # httpx 一次都没走

def test_a_dead_session_falls_back_to_the_cookie_path(self):
    self.session.alive = lambda: False
    be = self.make(session=self.session)
    be.get("10.1002/x")
    self.assertEqual(len(be.transport.requests), 1)
```

- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现两条分支**（`_open` 的 gate 判定保持共用，两种来源都过 `_is_gate`）。
- [ ] **Step 4: 跑用例确认通过**，再跑 `python3 -m unittest tests.test_wiley tests.test_gated_sites -v` 确认老路径未动。
- [ ] **Step 5: 提交** — `git commit -m "feat(gated): serve entitled pages through a live browser session"`

### Task 5: 让 `fetch` 真的拿到会话

**Files:**
- Modify: `litcli/config.py`（`[browser]` 段：`enabled`、`profile`、`headless`、`channel`、`proxy`）
- Modify: `litcli/backend_factory.py` 或 `config.py` 里建出版社后端处，给 `EntitledBackend` 传入按 `[browser]` 构造的 `Session`
- Modify: `config.example.toml`、`README.md`、`README.en.md`、`skills/litcli/SKILL.md`
- Test: `tests/test_browser_config.py`

**Interfaces:**
- Produces: `browser.session_from_config(cfg: dict) -> Session | None`（`[browser].enabled` 为假时返回 `None`；`proxy` 取 `[network].proxy`，`headless` 取 `[browser].headless`，`channel` 缺省 `"chrome"` 且找不到时降级为 `None` 并 `notice()` 告警）。

- [ ] **Step 1: 写失败用例** — `enabled = false` 返回 `None`；`proxy` 正确透传；找不到 Chrome 时 `channel is None`。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现并在建后处置入 `session=`**；文档三处与 `config.example.toml` 同步，写明「装好 Chrome + `litcli browser login` 一次，之后无需 cookie」。
- [ ] **Step 4: 跑用例通过 + `python3 -m unittest discover -s tests`** 全绿 + `ruff check litcli tests`。
- [ ] **Step 5: 提交** — `git commit -m "feat(fetch): take entitlement from the browser session when configured"`

---

## Phase 2 — 项目仓库与收网循环

### Task 6: 项目仓库（纯函数）

**Files:**
- Create: `litcli/project.py`
- Test: `tests/test_project.py`

**Interfaces:**
- Produces:
  - `@dataclass class Project: root: Path; question: str; seeds: list[str]; year_from: str; year_to: str; container: str; type_weight: dict[str, float]; threshold: float; effort: str; status: str; created: str`
  - `slugify(question: str) -> str` —— 取前 6 个词、`safe_filename` 清洗、空则 `survey`
  - `create(root: Path, question: str, **fields) -> Project`（`root` 已存在时加 `-2`、`-3` 后缀）
  - `load(root: Path) -> Project`、`save(project: Project) -> None`
  - `append_seen(project, record: dict)`、`append_trace(project, record: dict)`、`read_seen(project) -> list[dict]`、`read_trace(project) -> list[dict]`
  - `corpus_dir(project) -> Path`（负责 `mkdir`）
- 所有写用「同目录 `.tmp` → `os.replace`」；`load` 解析失败抛 `BadInput` 带**行号**与文件路径。

- [ ] **Step 1: 写失败用例** — 同一课题两次 `create` 得到 `q/x` 与 `q/x-2`；`append_seen` 后 `read_seen` 读回同样一条；往 `seen.jsonl` 塞半行后 `load` 的报文含 `seen.jsonl` 与该行号；`save` 前后 `project.toml` 可 `tomllib` 解析。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**（`project.toml` 键名：`question/seeds/year_from/year_to/container/type_weight/effort/status/created`）。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(project): on-disk survey project store"`

### Task 7: 档位表

**Files:**
- Create: `litcli/effort.py`
- Test: `tests/test_effort.py`

**Interfaces:**
- Produces: `LEVELS = ("low", "medium", "high", "max")`、`DEFAULT = "medium"`、`resolve(level: str, overrides: dict | None = None) -> dict`。返回值键：`frontier, admit, refs, citing, related, streak, openalex_cap, arxiv_cap, publisher_cap, max_hops`。

| level | frontier | admit | refs | citing | related | streak | openalex | arxiv | publisher | hops |
|---|---|---|---|---|---|---|---|---|---|---|
| low | 4 | 2 | 10 | 10 | 5 | 2 | 40 | 10 | 2 | 6 |
| medium | 8 | 5 | 20 | 20 | 10 | 3 | 200 | 40 | 6 | 20 |
| high | 16 | 8 | 30 | 30 | 15 | 4 | 600 | 100 | 15 | 50 |
| max | 24 | 12 | 50 | 50 | 25 | 6 | 1500 | 200 | 30 | 120 |

- [ ] **Step 1: 写失败用例** — 四档逐格对表；未知档位抛 `BadInput`；`resolve("low", {"admit": 9})` 的 `admit` 为 9、其余仍来自 low。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(project): effort levels as per-site envelopes"`

### Task 8: OpenAlex 带出打分要的数据

**Files:**
- Modify: `litcli/backends/openalex.py:23-30`（`_TYPE_MAP` 附近）、`:134`（`_work_to_item`）
- Test: `tests/test_openalex_stub.py`

**Interfaces:**
- Produces: 条目 `extra["openalex_type"]`（原词，不映射）与 `extra["referenced_works"]`（W-id 列表，无则 `[]`）。

- [ ] **Step 1: 写失败用例** — 喂一条 `{"type": "review", "referenced_works": ["W1", "W2"]}` 的 work，断言 `item.extra["openalex_type"] == "review"`、`item.extra["referenced_works"] == ["W1", "W2"]`，且 `item.item_type` 仍是原来的映射结果。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现两行填充**。
- [ ] **Step 4: 跑用例确认通过** + `tests.test_docs` 仍绿。
- [ ] **Step 5: 提交** — `git commit -m "feat(openalex): carry raw type and referenced_works for scoring"`

### Task 9: 打分（纯函数）

**Files:**
- Create: `litcli/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Produces: `sparse(project, trace) -> bool`（近 3 跳新增 < 2）、`score(item, project, collected_refs: set[str], trace: list[dict]) -> tuple[float, dict[str, float]]`。
- 分量先各自归一到 `0..1`，再乘权重：`c=3.0`、`b=1.0`、`a=0.5`（`sparse()` 为真时提到 `1.5`）、`d=project.type_weight.get(type, 0.0)`（缺省 1.0 for 声明过的类型，其余 0.2）。`c` = 与 `collected_refs` 的交集数 ÷ 20（封顶 1）；`b` = 年份在窗口内的线性位置，命中种子引用者再 `+0.5` 封顶 1；`a` = `cited_by_count / max(1, 2026 - year)` ÷ 20 封顶 1。

- [ ] **Step 1: 写失败用例** —— 一条与已收集合共被引 20 篇的 2020 年文献 `c` 分量为 1.0；窗口外年份 `b` 为 0；`review` 类型在 `type_weight={"review": 2.0}` 下总分为同条 0 权重时的两倍差；簇稀疏时 `a` 的权重升到 1.5。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**，返回的第二个值就是落 `seen.jsonl` 的分量字典。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): composite scoring with recorded parts"`

### Task 10: 扩展一跳

**Files:**
- Create: `litcli/expansion.py`
- Test: `tests/test_expansion.py`

**Interfaces:**
- Consumes: `Project`、`effort.resolve()` 的 `refs/citing/related`。
- Produces: `expand(item, backend, caps: dict, counters: dict) -> Iterator[tuple[AcademicItem, str]]`，第二个值是轴名（`refs`/`citing`/`related`）；某轴额度用尽或后端 `NotSupportedError` 时跳过该轴，`counters` 按轴扣减。

- [ ] **Step 1: 写失败用例** — 三个轴都返回条目时迭代出三条且轴名正确；`counters["citing"] = 0` 时不再调 `citing`；`NotSupportedError` 只吞掉该轴。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): per-axis expansion with envelopes"`

### Task 11: 准入判定

**Files:**
- Create: `litcli/admission.py`
- Test: `tests/test_admission.py`

**Interfaces:**
- Produces: `decide(item, project, seen: set[str], hop: int, caps: dict) -> dict`，键 `admit: bool`、`reason: str`、`identity: str`；种子的 `reason` 是 `seed`，其余为 `score 3.21 >= 2.50` 这类真实值。
- 顺序：身份（`aggregate.identity`）→ 已见 → 年份硬过滤（`seed` 豁免）→ `container` 子串 → 单跳上限 → 阈值（`project.threshold`，缺省 2.5）。

- [ ] **Step 1: 写失败用例** — 已见身份被拒且理由含 `seen`；窗口外年份被拒、同一条件加 `seed=True` 通过；达到单跳上限后后续全拒；每条拒绝都带非空 `reason`。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): admission with a recorded reason each"`

### Task 12: 事件与渲染

**Files:**
- Create: `litcli/survey_events.py`
- Test: `tests/test_survey_events.py`

**Interfaces:**
- Produces: `EVENTS = ("start","candidate","admit","download","hop_end","stop")`、`line(event: str, **fields) -> str`（一行 JSON，稳定键序：`event` 在最前）、`render_md(events: Iterable[dict]) -> str`（每个 `hop_end` 收一张表，列 `score / year / key / reason / title`）。

- [ ] **Step 1: 写失败用例** — `line()` 首字符序列化后以 `{"event": ` 开头且可 `json.loads`；未知事件名抛 `BadInput`；`render_md` 对两跳的输入产出两段表头、且 `unavailable` 的下载在表里标出。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): event schema and md rendering"`

### Task 13: `survey new`

**Files:**
- Create: `litcli/commands/survey.py`
- Modify: `litcli/commands/parser.py`（`survey` 族 + `new/run/status/stop`）、`litcli/cli.py`
- Test: `tests/test_survey_new.py`

**Interfaces:**
- Produces: `cmd_survey_new(cfg, args) -> int`，stdout 只打印 slug。种子来自 `--seed`；没给则按 `cfg` 默认后端检索一次取前 3 条（`extra["seed"]="search"`），一条都没有则 `BadInput`。`--year A-B` 拆成 `year_from`/`year_to`（只写 `A-B` 的两端，`A-` / `-B` 也认），每个 `--type T` 写 `type_weight[T] = 2.0`，`--container` 原样。

- [ ] **Step 1: 写失败用例**

```python
def test_filters_reach_the_project_file(self):
    cmd_survey_new(self.cfg, self.args("--seed", "10.1002/x", "--year", "2019-2024",
                                       "--type", "review", "--container", "Nature"))
    p = project.load(self.only_project())
    self.assertEqual((p.year_from, p.year_to), ("2019", "2024"))
    self.assertEqual(p.type_weight, {"review": 2.0})
    self.assertEqual(p.container, "Nature")
    self.assertEqual(p.threshold, 2.5)

def test_seeds_skip_the_search_and_an_empty_search_says_so(self):
    cmd_survey_new(self.cfg, self.args("--seed", "10.1002/x"))
    self.assertEqual(self.backend.search_calls, 0)
    self.backend.hits = []
    self.assertEqual(cmd_survey_new(self.cfg, self.args()), 2)
```
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): create a project with seeds"`

### Task 14: `survey run` 循环

**Files:**
- Modify: `litcli/commands/survey.py`
- Test: `tests/test_survey_run.py`

**Interfaces:**
- Consumes: Task 6/7/9/10/11/12 全部、`fulltext.resolve_fulltext()`、`cache.stats()`。
- Produces: `run_loop(project, deps) -> dict`（返回 `{"stop": reason, "admitted": n}`），`cmd_survey_run(cfg, args) -> int`。锁用 `project.root / "$.lock"`（存在即退 2），`stop` 读 `project.root / "$.stop"`。
- 同时给 `litcli/cache.py` 加 `stats() -> dict[str, int]`（键 `hits` / `misses`）与 `reset_stats() -> None`：`get()` 命中加 `hits`、未命中和 `put()` 加 `misses`。每跳开始 `reset_stats()`、结束把 `hits` 从本跳扣掉的额度里加回去（重试照扣）。
- `run` 收 `--effort LEVEL`，在档位表上重设并写回 `project.toml`。

- [ ] **Step 1: 写失败用例**

```python
def test_resume_does_not_expand_a_hop_twice(self):
    self.seed_trace_with_one_expanded_hop()
    run_loop(self.project, self.deps)
    self.assertEqual(self.backend.references_calls, 1)      # 只展开新前沿

def test_a_gate_on_download_keeps_the_candidate(self):
    self.deps.resolve = raising(BotWall("gate"))
    run_loop(self.project, self.deps)
    rec = self.seen_for("10.1002/x")
    self.assertTrue(rec["admit"]); self.assertEqual(rec["download"], "unavailable")

def test_streak_stop_is_reported_as_saturation(self):
    self.assertEqual(run_loop(self.project, self.deps)["stop"], "saturated")

def test_second_run_while_locked_exits_2(self):
    (self.project.root / "$.lock").write_text("1")
    self.assertEqual(cmd_survey_run(self.cfg, self.args), 2)

def test_a_browser_that_dies_mid_hop_falls_back_for_that_download(self):
    self.session.alive = iter([True, False]).__next__   # 活到准入，下载时已死
    run_loop(self.project, self.deps)
    self.assertEqual(self.seen_for("10.1002/x")["download"], "cookie")
```

- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现循环**：读检查点重建前沿 → 每跳 `cache.reset_stats()` 并按 `frontier` 取前 N → `expand` → `decide` → `corpus_dir` 下载（无 DOI 记 `no-doi`；浏览器死掉时该次改走 cookie 通路并记 `cookie`）→ 按 `cache.stats()` 的命中数退还本跳额度 → 写 `seen`/`trace` → `hop_end` → 判停（`streak`、各站额度、`max_hops`、`$.stop`、SIGINT）→ 每跳 `save` 检查点。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): resumable net-casting loop"`

### Task 15: `survey status` / `survey stop`

**Files:**
- Modify: `litcli/commands/survey.py`
- Test: `tests/test_survey_status.py`

**Interfaces:**
- Produces: `cmd_survey_status(cfg, args) -> int`（默认 JSON：`question/status/hops/admitted/last_stop/remaining`）、`cmd_survey_stop(cfg, args) -> int`（写 `$.stop`；已停时退 0 并说明）。

- [ ] **Step 1: 写失败用例** — `status` 输出可 `json.loads` 且含 `remaining`；`stop` 后 `$.stop` 存在；对已停项目再 `stop` 退 0。
- [ ] **Step 2: 跑用例确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑用例确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(survey): status and stop"`

### Task 16: 端到端离线用例

**Files:**
- Test: `tests/test_survey_e2e.py`

**Interfaces:**
- Consumes: 全部。

- [ ] **Step 1: 写用例** — 用 `FakeSession` + `stub()` 的 OpenAlex 后端跑 `low` 档两跳：断言 `corpus/` 有文件、`seen.jsonl` 每条都有 `reason`、`trace.jsonl` 的额度严格递减且**同一批 key 第二次出现（`cache.stats()["hits"] > 0`）时该跳不递减**、`--format md` 输出含表头。
- [ ] **Step 2: 跑用例确认失败**（此时多半是额度记账那一处）。
- [ ] **Step 3: 修到通过**。
- [ ] **Step 4: 跑 `python3 -m unittest discover -s tests` 与 `ruff check litcli tests`** 全绿。
- [ ] **Step 5: 提交** — `git commit -m "test(survey): offline end-to-end over a stub graph"`

### Task 17: 文档、技能、补全、CHANGELOG

**Files:**
- Modify: `README.md`、`README.en.md`、`skills/litcli/SKILL.md`、`config.example.toml`、`CHANGELOG.md`、`litcli/completions.py`

- [ ] **Step 1: 四处文档写 `survey` 命令面、`browser login`、`[browser]` 配置与档位表**；`CHANGELOG.md` 的 `[Unreleased]` 加 Added 条目。
- [ ] **Step 2: `completions.py` 的 `BACKENDS` 旁边补 `SURVEY_SUBS` / `EFFORT_LEVELS` 取值补全**。
- [ ] **Step 3: 跑 `python3 -m unittest tests.test_docs tests.test_skill_commands tests.test_completions -v`** 全绿。
- [ ] **Step 4: 提交** — `git commit -m "docs(survey): commands, config and changelog"`

---

## 验收

- `python3 -m unittest discover -s tests` 全绿；`ruff check litcli tests` 干净。
- 活体验证（人工，一次性）：`litcli browser login ieee` 后 `litcli fetch 10.1109/…` 拿到 `%PDF`，全程不填 cookie。
- 活体验证（人工）：`litcli survey new "…" --effort low` → `litcli survey run <slug>` 跑到饱和，`corpus/` 有产出、`seen.jsonl` 每条有理由。
