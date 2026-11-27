# 浏览器所有者（broker）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把“谁拥有这只 Chrome”从每条命令手里的 `/proc` 清场，改成一个按 profile 唯一的常驻 broker：并行命令接同一只浏览器、各开自己的页、没人用才退；用户自己的 Chrome 可直接接上。

**Architecture:** 新模块 `litcli/browser_broker.py` 同时装服务端（`Broker`：控制口 + 锁 + 三态判定 + 拉起/接上 + 空闲退出）与客户端（`ensure_lease` / `Lease`）。数据面不变：客户端拿 `cdp_port` 自己 `connect_over_cdp`，`Session.page` / `download` 原样保留。`Session` 只剩客户端语义，`_running_on` / `_reap` / `_launch_detached` 等搬进 broker。

**Tech Stack:** 标准库 `socket` / `selectors` / `fcntl`(`msvcrt`) / `subprocess` / `json`；Playwright `connect_over_cdp`；`unittest`（非 pytest）；ruff。

**Spec:** `docs/superpowers/specs/2026-11-27-browser-broker-design.md`

## Global Constraints

- 所有 `.py` 文本（注释、docstring、用户可见字符串）一律英文；`README.md` / `README.en.md` / `SKILL.md` / `config.example.toml` / `CHANGELOG.md` 保持文档语言（中文/英文成对）。
- 一次改动只留一条提交；审查修复用 `commit --amend`，不补 `fix:` 提交。
- 测试是 `python3 -m unittest`；新增/改动文件必须 `ruff check litcli tests` 干净。
- **导入方向**：`litcli/browser.py` 在模块层 `from . import browser_broker`；`browser_broker.py` 需要 `browser.py` 的 `cdp_ready` / `_attach_engine` / `BrowserUnavailable` 时**在函数体内局部导入**（`from .browser import cdp_ready`），避免循环导入。
- 只绑 `127.0.0.1`；发现文件 `chmod 0600`。
- 默认空闲退出 `[browser].idle_minutes = 10`，`0` 表示不按空闲退；启动等待上限 20 秒；登录等租约清零上限 30 秒；`DevToolsActivePort` 试连上限 2 秒。
- profile 解析：空或 `user` → 平台用户默认 profile（Linux `~/.config/google-chrome`、Windows `%LOCALAPPDATA%\Google\Chrome\User Data`、macOS `~/Library/Application Support/Google/Chrome`）；`dedicated` → `paths.browser_profile_dir()`；其它按 `~` 展开。`[browser].profile_directory` 默认 `Default`，只在显式设置时传 `--profile-directory`。
- 用户那只浏览器一律不杀：`owns=False` 的空闲退出与 `browser stop` 都只断连；`owns=True` 且**有头**的 `stop` 也不杀（打印 pid 与 “close the window to end it”）；只有 `owns=True` 且**无头**的才关。`_reap` 只杀带 `--headless` 的，以及 `dedicated` 目录里的全部。
- 被占且没开调试口时退 **1**，文案逐字（常量 `_BUSY_SENTENCE`）：
  `your Chrome is already running on this profile without a debugging port - close it, or start it with --remote-debugging-port=0`
- 绝不导航别人已有的页（`context.pages[0]`）；会话只用 `context.new_page()`。
- 版本：broker 与 CLI 同版本；不符按“无租约则替换、有租约则沿用并 `notice` 一次”。
- Windows：`DETACHED_PROCESS` 起子进程、`msvcrt.locking` 加锁、无 `/proc` 以控制口与子进程退出码判定；真机验证标**待核**。
- 不做 cookie 库拷贝 / `storage_state` 交接。
- 不发版（除非另行要求）。

## Review Focus

1. 用户 Chrome 开着、没带调试口：命令退 1 并给出那句文案，**不是** `start()` 成功、随后 `TargetClosedError`。（Task 4 的 `test_a_busy_profile_surfaces_as_exit_1_with_the_sentence`）
2. 接在用户浏览器上时 `context.pages[0]` 的地址跑完不变（他们的页没被导航走）。（Task 6 的 `test_each_session_gets_its_own_page_and_never_pages_zero`）
3. 陈旧 `DevToolsActivePort`（文件在、端口无人听）不被当成可接。（Task 3 的 `test_a_stale_port_file_is_not_attached`）
4. 空闲退出与 `browser stop` 不杀 `owns=False` 与 `owns=True`+有头的浏览器（含“空闲撞上新客户端”那一秒）。（Task 2 的 `test_an_idle_broker_exits_and_a_pinned_one_does_not`、`test_an_idle_exit_backs_off_when_the_lock_is_busy`，Task 8 的 `test_an_attached_browser_is_left_running`）
5. 两个并发命令各拿自己的页、互不打断：并行下载都完整，收尾时谁都不许杀 broker。（Task 6 的 `test_close_releases_the_lease_before_the_page`、Task 10 脚本的验收 1）

---

### Task 1: 控制协议与服务端骨架

**Files:**
- Create: `litcli/browser_broker.py`
- Test: `tests/test_browser_broker.py`

**Interfaces:**
- Consumes: `litcli.browser.BrowserUnavailable`、`litcli.paths.browser_session_file()`。
- Produces:
  - `PROTOCOL_VERSION = 1`（整数；`hello`/`status` 都带，客户端比对）
  - `class Backend(NamedTuple): port: int; owns: bool; stop: Callable[[], None] | None`
  - `class Broker.__init__(self, *, profile: Path, headless=False, profile_directory=None, source="default", pinned=False, idle_minutes=10.0, backend=None, clock=time.monotonic)`；`backend: () -> Backend` 供测试注入，缺省由 Task 3 接上 `prepare`
  - `Broker.bind(host, port) -> None`（测试先绑好取端口）、`Broker.control_port: int`
  - `Broker.serve() -> int`：调 `backend()` → 写发现文件 → 事件循环；返回进程退出码
  - `Broker.stop() -> None`：关监听、退出循环（测试与 `shutdown` 用；幂等，已退出再调不报错）
  - `Broker.leases: int`、`Broker.tick() -> None`（一次空闲/关停判定）、`Broker.exited: bool`
  - `class BrokerUnavailable(BrowserUnavailable)`；`class ProfileBusy(BrowserUnavailable)`（`exit_code = 1`）
  - 报文：`hello` → `{cdp_port,broker_pid,headless,pinned,owns,version}`，该连接转为租约；`status` → `{cdp_port,pid,headless,leases,idle_s,version,profile,profile_directory,source,owns}`；`shutdown{force}` → `{ok,stopped}`；`pin{pinned}` → `{ok,pinned}`；未知命令 → `{"error":...}` 并关该连接
  - 发现文件字段：`{pid,control_port,cdp_port,profile,profile_directory,source,owns,version,headless,pinned,started,leases}`，权限 `0600`

- [ ] **Step 1: 写失败测试**

`tests/test_browser_broker.py`：

```python
"""The broker's control port: one line per command, EOF is a released lease."""

import json, os, socket, tempfile, threading, time, unittest
from unittest import mock

from litcli import browser_broker as bb
from litcli import paths


def _serve(backend=lambda: bb.Backend(41_000, True, None), **kw):
    """Run a broker in a thread; return (broker, control_port)."""
    b = bb.Broker(profile=paths.browser_profile_dir(), **kw, backend=backend)
    b.bind("127.0.0.1", 0)
    threading.Thread(target=b.serve, daemon=True).start()
    for _ in range(100):
        if paths.browser_session_file().exists():
            break
        time.sleep(0.02)
    return b, b.control_port


def _line(port, msg, timeout=2.0):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall((json.dumps(msg) + "\n").encode())
        return json.loads(s.makefile().readline())


def _hold(port, version=bb.PROTOCOL_VERSION):
    """Open a hello connection and keep it (one lease); the caller closes it."""
    s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    s.sendall((json.dumps({"cmd": "hello", "version": version,
                           "headless": True, "pid": 1}) + "\n").encode())
    s.makefile().readline()
    return s


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self._home.name})
        patcher.start(); self.addCleanup(patcher.stop)
        paths.browser_session_file().parent.mkdir(parents=True, exist_ok=True)

    def test_hello_answers_the_cdp_port_and_becomes_a_lease(self):
        b, port = _serve()
        try:
            answer = _line(port, {"cmd": "hello", "version": bb.PROTOCOL_VERSION,
                                  "headless": True, "pid": 1})
            self.assertEqual(answer["cdp_port"], 41_000)
            self.assertTrue(answer["owns"])
            self.assertEqual(b.leases, 1)
        finally:
            b.stop()

    def test_closing_the_hello_connection_releases_the_lease(self):
        b, port = _serve()
        try:
            s = _hold(port)
            self.assertEqual(b.leases, 1)
            s.close()
            for _ in range(100):
                if b.leases == 0:
                    break
                time.sleep(0.02)
            self.assertEqual(b.leases, 0)
        finally:
            b.stop()

    def test_an_unknown_command_is_one_error_line_and_the_others_survive(self):
        b, port = _serve()
        try:
            self.assertIn("error", _line(port, {"cmd": "wat"}))
            lease = _hold(port)
            status = _line(port, {"cmd": "status"})
            self.assertEqual(status["leases"], 1)
            self.assertEqual(status["profile"], str(paths.browser_profile_dir()))
            lease.close()
        finally:
            b.stop()

    def test_status_names_the_profile_directory(self):
        b, port = _serve(profile_directory="Profile 1")
        try:
            self.assertEqual(_line(port, {"cmd": "status"})["profile_directory"], "Profile 1")
        finally:
            b.stop()
```

- [ ] **Step 2: 跑测试，确认失败**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'litcli.browser_broker'`

- [ ] **Step 3: 实现骨架**

`selectors` 单线程循环，每行一条 JSON；`hello` 成功后把连接注册进 `self._leases`，EOF 时移除并记 `self._last_release = self._clock()`；`status` 的 `idle_s = 0 if self.leases else self._clock() - self._last_release`；`stop()` 设 `self._stopping`，循环的 `selector.select()` 用 0.2 秒超时以便及时看到它；`tick()` 每次调用做一次检查（Task 2 填内容，本任务只需存在）。写发现文件用 `os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)` + `json.dumps`；`broker_pid` 用 `os.getpid()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: PASS（4 个用例）

- [ ] **Step 5: 提交**

```bash
git add litcli/browser_broker.py tests/test_browser_broker.py
git commit -m "feat(browser): a broker that hands out CDP ports against leases"
```

---

### Task 2: 锁、空闲退出与关停

**Files:**
- Modify: `litcli/browser_broker.py`
- Test: `tests/test_browser_broker.py`

**Interfaces:**
- Consumes: Task 1 的 `Broker`。
- Produces:
  - `@contextlib.contextmanager def profile_lock(profile: Path, timeout: float = 2.0)` —— 在 `<profile 的父目录>/<profile 名>.lock` 上加非阻塞排他锁并轮询到超时；POSIX `fcntl.flock`，Windows `msvcrt.locking`；超时抛 `TimeoutError`
  - `Broker.pinned: bool`、`Broker.pin(pinned: bool = True) -> bool`
  - 空闲（`tick()` 内）：`not self._stopping` 且 `leases == 0` 且 `idle_minutes > 0` 且 `idle_s >= idle_minutes*60` 且 `not pinned` → 在**锁里**复查租约数，仍为 0 才退出；拿不到锁（`TimeoutError`）本次不退出
  - `shutdown`：同样在**锁里**判定——`force`，或锁内复查 `leases == 0`，才退出；退出时回 `{"ok": true, "stopped": true}`，否则 `{"ok": true, "stopped": false}`。锁的意义是：新客户端在拿到锁之前发不出 `hello`，离开的一方不能在它脚下关掉 broker

- [ ] **Step 1: 写失败测试**

```python
class LifecycleTest(BrokerTest):
    def setUp(self):
        super().setUp()
        self.clock = mock.Mock(return_value=1000.0)

    def test_the_discovery_file_is_written_private_and_complete(self):
        b, port = _serve(clock=self.clock)
        try:
            info = json.loads(paths.browser_session_file().read_text())
            self.assertEqual(info["control_port"], port)
            self.assertEqual(info["leases"], 0)
            self.assertFalse(info["pinned"])
            self.assertEqual(info["pid"], os.getpid())
            self.assertEqual(paths.browser_session_file().stat().st_mode & 0o777, 0o600)
        finally:
            b.stop()

    def test_an_idle_broker_exits_and_a_pinned_one_does_not(self):
        for pinned, expected in ((False, True), (True, False)):
            self.clock.return_value = 1000.0
            b, _port = _serve(clock=self.clock, pinned=pinned)
            self.clock.return_value = 1000.0 + 11 * 60
            b.tick()
            self.assertEqual(b.exited, expected)
            b.stop()

    def test_zero_idle_minutes_never_exits(self):
        b, _port = _serve(clock=self.clock, idle_minutes=0)
        try:
            self.clock.return_value = 10**6
            b.tick()
            self.assertFalse(b.exited)
        finally:
            b.stop()

    def test_an_idle_exit_backs_off_when_the_lock_is_busy(self):
        b, _port = _serve(clock=self.clock)
        try:
            self.clock.return_value = 1000.0 + 11 * 60
            with mock.patch.object(bb, "profile_lock", side_effect=TimeoutError):
                b.tick()
            self.assertFalse(b.exited)
        finally:
            b.stop()

    def test_shutdown_waits_for_leases_unless_forced(self):
        b, port = _serve()
        try:
            lease = _hold(port)
            self.assertFalse(_line(port, {"cmd": "shutdown", "force": False})["stopped"])
            self.assertTrue(_line(port, {"cmd": "shutdown", "force": True})["stopped"])
            lease.close()
        finally:
            b.stop()

    def test_pin_sets_and_clears(self):
        b, port = _serve()
        try:
            self.assertTrue(_line(port, {"cmd": "pin"})["pinned"])
            self.assertFalse(_line(port, {"cmd": "pin", "pinned": False})["pinned"])
        finally:
            b.stop()

    def test_the_lock_is_exclusive(self):
        with bb.profile_lock(paths.browser_profile_dir()):
            with self.assertRaises(TimeoutError):
                with bb.profile_lock(paths.browser_profile_dir(), timeout=0.2):
                    pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: FAIL —— `AttributeError: module 'litcli.browser_broker' has no attribute 'profile_lock'`

- [ ] **Step 3: 实现**

`profile_lock` 用 `os.open(lock, O_CREAT|O_RDWR, 0o600)` + 0.05 秒一档的非阻塞轮询，退出时关 fd。`tick()` 是空闲判定的唯一入口，`serve()` 的循环每 1 秒调一次；退出时删发现文件、关监听、调 `backend.stop`（Task 3 接上）。`shutdown` 处理函数按上面的规则设 `self._stopping`；先回包，下一轮退出。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/browser_broker.py tests/test_browser_broker.py
git commit -m "feat(browser): lock, idle and shutdown decisions"
```

---

### Task 3: 三态判定与浏览器拉起

**Files:**
- Modify: `litcli/browser_broker.py`（从 `litcli/browser.py:151-331` 搬入 `_running_on` / `_reap` / `_wait_gone` / `_is_headless` / `_free_port` / `_launch_detached` / `_default_launcher`）
- Modify: `litcli/browser.py:151-331`（搬走的定义删除，`browser.py` 不再导出租）
- Test: `tests/test_browser_broker.py`

**Interfaces:**
- Consumes: Task 2 的 `profile_lock`；`browser.py` 的 `cdp_ready` / `_attach_engine`（局部导入）。
- Produces:
  - `_BUSY_SENTENCE: str`（Global Constraints 里那句逐字文案）
  - `class BrowserRuntime: port: int; stop: Callable[[], None] | None; pid: int | None = None`（`_Runtime` 搬过来的精简形态，测试可直接构造）
  - `def live_port(profile: Path) -> int | None` —— 读 `<profile>/DevToolsActivePort` 第一行，`cdp_ready(port, timeout=2.0)` 为真才返回，否则 `None`
  - `def profile_holder(profile: Path) -> int | None` —— 解析 `SingletonLock`（`hostname-pid`，只认本机主机名），`os.kill(pid, 0)` 成功才返回；读不到或格式不合返回 `None`
  - `def _binary(channel: str | None) -> str` —— 先 `shutil.which(channel)`，再逐个 `shutil.which(n) for n in _CHROME_BINARIES`；都没有则 `_bundled_binary()`
  - `def _bundled_binary() -> str` —— 起一次 Playwright 取 `chromium.executable_path` 后停掉；缺 Playwright 抛 `BrowserUnavailable`（沿用旧的 “Playwright is not installed … install-browser” 文案）
  - `def prepare(profile, headless, profile_directory, channel, proxy) -> Backend` —— `live_port` 有值 → `Backend(port, False, stop)`；否则 `profile_holder` 有值 → 抛 `ProfileBusy(_BUSY_SENTENCE)`；否则 `_launch_detached(_binary(channel), ...)` → `Backend(rt.port, True, rt.stop)`
  - `_launch_detached(binary, profile, headless, proxy, profile_directory=None)`；子进程**以 0 退出**后 `live_port` 仍为 `None` → 同样抛 `ProfileBusy`
  - `_reap(profile, keep)` 收窄：只杀带 `--headless` 的；`profile` 等于 `paths.browser_profile_dir()` 时全杀

- [ ] **Step 1: 写失败测试**

```python
class PrepareTest(BrokerTest):
    def test_a_live_devtools_port_is_attached_without_launching(self):
        (paths.browser_profile_dir() / "DevToolsActivePort").write_text("5555\n/x\n")
        with mock.patch.object(bb, "cdp_ready", return_value=True) as ready, \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            out = bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        self.assertFalse(out.owns)
        self.assertEqual(out.port, 5555)
        self.assertEqual(ready.call_args.args[0], 5555)

    def test_a_stale_port_file_is_not_attached(self):
        (paths.browser_profile_dir() / "DevToolsActivePort").write_text("5555\n/x\n")
        with mock.patch.object(bb, "cdp_ready", return_value=False), \
                mock.patch.object(bb, "profile_holder", return_value=None), \
                mock.patch.object(bb, "_launch_detached",
                                  return_value=bb.BrowserRuntime(7123, None)) as launch:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        launch.assert_called_once()

    def test_a_held_profile_without_a_port_raises_the_exact_sentence(self):
        with mock.patch.object(bb, "live_port", return_value=None), \
                mock.patch.object(bb, "profile_holder", return_value=4242), \
                self.assertRaises(bb.ProfileBusy) as ctx:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(str(ctx.exception), bb._BUSY_SENTENCE)

    def test_the_profile_directory_and_headless_flag_reach_the_command_line(self):
        with mock.patch.object(bb.subprocess, "Popen") as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            popen.return_value.poll.return_value = None
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, "Profile 1")
        argv = popen.call_args.args[0]
        self.assertIn("--profile-directory=Profile 1", argv)
        self.assertIn("--headless=new", argv)

    def test_without_any_chrome_the_bundled_browser_is_launched(self):
        with mock.patch.object(bb.shutil, "which", return_value=None), \
                mock.patch.object(bb, "_bundled_binary", return_value="/b/chrome"), \
                mock.patch.object(bb, "live_port", return_value=None), \
                mock.patch.object(bb, "profile_holder", return_value=None), \
                mock.patch.object(bb, "_launch_detached",
                                  return_value=bb.BrowserRuntime(7123, None)) as launch:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        self.assertEqual(launch.call_args.args[0], "/b/chrome")

    def test_reap_kills_headless_leftovers_but_not_headed_ones_on_a_user_profile(self):
        elsewhere = paths.browser_profile_dir().parent / "elsewhere"
        with mock.patch.object(bb, "_running_on", return_value=[(1, None), (2, None)]), \
                mock.patch.object(bb, "_is_headless", side_effect=lambda pid: pid == 1), \
                mock.patch.object(bb.os, "kill") as killed:
            bb._reap(elsewhere, keep=None)
            bb._reap(paths.browser_profile_dir(), keep=None)
        pids = [call.args[0] for call in killed.call_args_list]
        self.assertEqual(pids.count(1), 2)   # headless: killed either way
        self.assertEqual(pids.count(2), 1)   # headed: only in the dedicated dir
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: FAIL —— `AttributeError: module 'litcli.browser_broker' has no attribute 'prepare'`

- [ ] **Step 3: 实现并接线**

搬代码改三处：`_launch_detached` 加 `profile_directory` 参数并在非空时 `argv.insert(1, f"--profile-directory={value}")`；`_reap` 按上面的判据收窄；`_binary` / `_bundled_binary` 取代 `_default_launcher` 的二进制选取（旧的进程内回落整段删掉，协商好的浏览器一律分离启动）。`BrowserRuntime` 是搬来的 `_Runtime` 精简形态。`Broker.serve()`：`self._backend = self._backend or prepare(...)`，`ProfileBusy` 时把消息打到 `sys.stderr` 并 `return 1`。`Broker` 退出时先调 `self._backend.stop`，再按 `owns and headless` 决定是否 SIGTERM，发完用 `_wait_gone` 等它真的消失（本任务先只做这一支）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/browser_broker.py litcli/browser.py tests/test_browser_broker.py
git commit -m "feat(browser): the broker decides attach, report or launch"
```

---

### Task 4: 客户端 `ensure_lease`、`connect` 与 `Lease`

**Files:**
- Modify: `litcli/browser_broker.py`
- Test: `tests/test_browser_broker.py`

**Interfaces:**
- Consumes: Task 1–3。
- Produces:
  - `class Lease: cdpport: int; owns: bool; pinned: bool; version: int; live() -> bool; close() -> None`
  - `def connect(control_port, *, version=PROTOCOL_VERSION, headless, timeout=2.0) -> Lease` —— 发 `hello`、读答复；超时/关闭抛 `BrokerUnavailable`
  - `def read_session() -> dict | None` —— 读发现文件，坏文件当 `None`
  - `def shutdown(control_port, *, force=False) -> dict`、`def pin(control_port, *, pinned=True) -> dict` —— 一发一收的短命令
  - `def ensure_lease(profile, *, headless, profile_directory=None, source="default", idle_minutes=10.0, channel=None, proxy=None, pinned=False, version=PROTOCOL_VERSION, wait=20.0, spawn=subprocess.Popen) -> Lease`：
    1. `read_session()` 有控制口 → `connect`；成功即返回
    2. 失败或没有 → `with profile_lock(profile, timeout=wait)`：重读再试一次；仍无 → `_spawn_broker(...)`，轮询发现文件到 `wait` 到期
    3. 子进程以非 0 退出且 stderr 非空 → 首行含 `without a debugging port` 时转 `ProfileBusy`，其它转 `BrokerUnavailable`（都逐字带上）
    4. 到期无文件 → `BrokerUnavailable("the browser broker did not come up ...")`

- [ ] **Step 1: 写失败测试**

```python
def _spawn_that_fails(stderr, code=1):
    def spawn(argv, **kw):
        return mock.Mock(returncode=code, poll=lambda: code,
                         communicate=lambda timeout=None: (b"", stderr.encode()))
    return spawn


def _spawn_that_brings_up(port):
    def spawn(argv, **kw):
        threading.Timer(0.05, lambda: paths.browser_session_file().write_text(json.dumps(
            {"pid": os.getpid(), "control_port": port, "cdp_port": 41_000,
             "version": bb.PROTOCOL_VERSION}))).start()
        return mock.Mock(poll=lambda: None)
    return spawn


class EnsureLeaseTest(BrokerTest):
    def test_a_running_broker_is_joined(self):
        b, port = _serve()
        try:
            paths.browser_session_file().write_text(json.dumps(
                {"pid": os.getpid(), "control_port": port, "cdp_port": 41_000,
                 "version": bb.PROTOCOL_VERSION}))
            lease = bb.ensure_lease(paths.browser_profile_dir(), headless=True)
            self.assertEqual(lease.cdpport, 41_000)
            lease.close()
        finally:
            b.stop()

    def test_a_dead_broker_is_replaced(self):
        paths.browser_session_file().write_text(json.dumps(
            {"pid": 1, "control_port": 1, "version": bb.PROTOCOL_VERSION}))
        b, port = _serve()
        try:
            paths.browser_session_file().unlink()   # the fresh broker will write its own
            with mock.patch.object(bb, "_spawn_broker", side_effect=_spawn_that_brings_up(port)):
                lease = bb.ensure_lease(paths.browser_profile_dir(), headless=True)
            self.assertEqual(lease.cdpport, 41_000)
            lease.close()
        finally:
            b.stop()

    def test_a_busy_profile_surfaces_as_exit_1_with_the_sentence(self):
        with mock.patch.object(bb, "_spawn_broker",
                               side_effect=_spawn_that_fails(bb._BUSY_SENTENCE)), \
                self.assertRaises(bb.ProfileBusy) as ctx:
            bb.ensure_lease(paths.browser_profile_dir(), headless=True)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertIn("without a debugging port", str(ctx.exception))

    def test_no_broker_by_the_deadline_is_unavailable(self):
        with mock.patch.object(bb, "_spawn_broker", return_value=mock.Mock(poll=lambda: None)):
            with self.assertRaises(bb.BrokerUnavailable):
                bb.ensure_lease(paths.browser_profile_dir(), headless=True, wait=0.3)

    def test_live_follows_the_socket(self):
        b, port = _serve()
        try:
            paths.browser_session_file().write_text(json.dumps(
                {"pid": os.getpid(), "control_port": port, "cdp_port": 41_000,
                 "version": bb.PROTOCOL_VERSION}))
            lease = bb.ensure_lease(paths.browser_profile_dir(), headless=True)
            self.assertTrue(lease.live())
            lease.close()
            self.assertFalse(lease.live())
        finally:
            b.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: FAIL —— `AttributeError: ... no attribute 'ensure_lease'`

- [ ] **Step 3: 实现**

`Lease.live()` 在同一 socket 上发 `{"cmd":"status"}` 并等 1 秒；`close()` 只是 `sock.close()`（EOF 即释放）。`_spawn_broker(argv, **kw)` 是 `subprocess.Popen` 的薄包装（`stdout=DEVNULL`、`stderr=PIPE`；Windows 加 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，否则 `start_new_session=True`）。argv：`[sys.executable, "-m", "litcli.browser_broker", "--profile", str(profile)]` 加可选 `--profile-directory/--source/--headless/--channel/--proxy/--pinned/--idle-minutes`。`main(argv)` 用 `argparse` 解析后构造 `Broker` 并 `sys.exit(broker.serve())`；`pinned` 同时决定 broker 跳过空闲退出。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/browser_broker.py tests/test_browser_broker.py
git commit -m "feat(browser): join or spawn a broker, surfacing a busy profile"
```

---

### Task 5: profile 解析、`user` 默认与迁移提示

**Files:**
- Modify: `litcli/paths.py:127-138`
- Modify: `litcli/browser.py:338-354`（`session_from_config`）与 `Session.__init__` 的 profile 参数
- Modify: `config.example.toml:235-239`
- Test: `tests/test_browser_config.py`

**Interfaces:**
- Produces:
  - `paths.user_browser_profile_dir() -> Path`（按 Global Constraints 的平台路径）
  - `paths.resolve_browser_profile(value: str | None) -> Path`
  - `Session.__init__` 的 `profile` 类型改为 `str | None`，内部 `paths.resolve_browser_profile`；新增 `profile_directory: str = "Default"` 与 `profile_source: str = "default"`，落到 `self._profile_directory` / `self._profile_source`
  - `session_from_config` 在“未设 `profile` 且 `browser_profile_dir()/Default/Cookies` 非空”时 `debug.notice(...)` 一次，文案含 `` `profile = "dedicated"` ``

- [ ] **Step 1: 写失败测试**

```python
class ResolveProfileTest(unittest.TestCase):
    def test_empty_and_user_resolve_to_the_platform_default(self):
        self.assertEqual(paths.resolve_browser_profile(None), paths.user_browser_profile_dir())
        self.assertEqual(paths.resolve_browser_profile("user"), paths.user_browser_profile_dir())

    def test_dedicated_resolves_to_the_tools_own_dir(self):
        self.assertEqual(paths.resolve_browser_profile("dedicated"), paths.browser_profile_dir())

    def test_a_path_is_expanded(self):
        self.assertEqual(paths.resolve_browser_profile("~/elsewhere"),
                         Path("~/elsewhere").expanduser())

    def test_a_disabled_section_still_yields_none(self):
        self.assertIsNone(session_from_config({"browser": {"enabled": False}}))

    def test_the_source_is_reported(self):
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"):
            default = session_from_config({"browser": {"enabled": True}})
            explicit = session_from_config({"browser": {"enabled": True, "profile": "dedicated"}})
        self.assertEqual(default._profile_source, "default")
        self.assertEqual(explicit._profile_source, "config")

    def test_the_migration_notice_fires_only_while_the_key_is_unset(self):
        cookie_db = paths.browser_profile_dir() / "Default" / "Cookies"
        cookie_db.parent.mkdir(parents=True); cookie_db.write_text("SQLite")
        with mock.patch("shutil.which", return_value="/usr/bin/google-chrome"), \
                mock.patch("litcli.debug.notice") as told:
            session_from_config({"browser": {"enabled": True}})
            session_from_config({"browser": {"enabled": True, "profile": "dedicated"}})
        told.assert_called_once()
        self.assertIn('profile = "dedicated"', told.call_args.args[0])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser_config -v`
Expected: FAIL —— `AttributeError: module 'litcli.paths' has no attribute 'resolve_browser_profile'`

- [ ] **Step 3: 实现**

`paths` 两个纯函数；`session_from_config` 传 `section.get("profile")`、`section.get("profile_directory") or "Default"`，`profile_source` 由“是否设了 `profile`”推出。`config.example.toml` 注释改为：`profile` 空=用你自己的 Chrome，`dedicated`=本工具的独立 profile，`profile_directory` 用于多 profile 的安装。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_config -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/paths.py litcli/browser.py config.example.toml tests/test_browser_config.py
git commit -m "feat(browser): default to the user's own profile, with a migration notice"
```

---

### Task 6: `Session` 改成租约客户端

**Files:**
- Modify: `litcli/browser.py:390-631`（`Session` 生命周期整段）
- Modify: `tests/fake_browser.py`（新增 `FakeLease`；`FakeRuntime` 改成 `new_page()` 真的新建页、`pages[0]` 是启动前就存在的那张）
- Modify: `tests/test_browser.py`（`SessionTest` 改 `lease_factory=`）
- Modify: `tests/test_survey_run.py`（`DriverError` → `browser_broker.BrokerUnavailable`）
- Modify: `tests/test_browser.py` 的 `MissingPlaywrightTest`（错误改由 `browser_broker._bundled_binary` 抛，断言同一句 “Playwright is not installed”）

**Interfaces:**
- Consumes: `browser_broker.ensure_lease` / `connect` / `read_session` / `shutdown` / `Lease`。
- Produces（`Session` 新形状）：
  - `__init__(self, profile: str | None = None, headless=False, channel=None, proxy=None, profile_directory="Default", profile_source="default", pinned=False, lease_factory=None)`
  - `ensure()` 把 `pinned`、`source=self._profile_source`、`profile_directory=self._profile_directory` 一并交给 `ensure_lease`
  - `self._lease: Lease | None`、`self._page_: object | None`
  - `mode` 属性：无租约 `"none"`；`lease.owns` 为假 `"attached"`；否则 `"launched"`
  - `alive() -> bool`：有租约看 `lease.live()`；无租约则 `read_session()` + `connect` 探一次后立刻 `close()`
  - `ensure()`：`lease_factory(...)` 或 `ensure_lease(...)`；版本不符时无租约 `shutdown(force=True)` 后重拉一次，有租约 `debug.notice(...)` 一次
  - `start()` = `ensure()` + `self._runtime = _Runtime(*_attach_engine(lease.cdpport), lease.cdpport)`（`_Runtime` 是客户端的 `(context, stop, port)`，与 broker 侧的 `BrowserRuntime` 不是一回事）
  - `_page()`：`context.new_page()` 一次并记 `self._page_`
  - `reset()`：只把自己那张页归 `about:blank`（没有就 `_page()` 后归），不碰别人的页
  - `close()`：记住 `port = self._lease.cdpport if self._lease else None`，先 `lease.close()`，再关自己的页与引擎
  - `terminate()`：按上一步取出 `port` 并 `close()`，`port` 非空时 `shutdown(port, force=True)`
  - `TargetClosedError`：`_drop_runtime()`（同时清 `self._page_`）→ `ensure()` → 重绑 context 与 `_request_context` → 重试一次，再败抛 `BrowserUnavailable`

- [ ] **Step 1: 写失败测试**

`tests/fake_browser.py` 加：

```python
class FakeLease:
    def __init__(self, cdpport=41_000, owns=False, pinned=False):
        self.cdpport, self.owns, self.pinned = cdpport, owns, pinned
        self.alive_flag = True
    def live(self):
        return self.alive_flag
    def close(self):
        self.alive_flag = False
```

并把 `FakeRuntime` 改成“启动前就有一张 `pages[0]`、`new_page()` 每次真新建一张”，`self.page` 指向最新一张（`goto` 的断言都打在它上面）：

```python
class FakeRuntime:
    def __init__(self, queue=None):
        self.queue = [] if queue is None else queue
        self.pages = [FakePage(self.queue)]   # the tab that existed before the session
        self.page = self.pages[0]
        self.request = FakeRequest()
        self.closed = False
        self.terminated = False
    def new_page(self):
        self.page = FakePage(self.queue)
        self.pages.append(self.page)
        return self.page
    # ...其余（set_default_timeout / close / terminate / stop）不变
```

`tests/test_browser.py` 的 `LeaseClientTest`（`_attach_engine` 一律 `mock.patch` 挡掉，返回 `(self.context, self.stop)`）：

```python
class LeaseClientTest(unittest.TestCase):
    def setUp(self):
        # 临时 XDG_DATA_HOME，同其它用例
        self.lease = FakeLease()
        self.runtime = FakeRuntime()
        self.stop = mock.Mock()
        attach = mock.patch.object(browser, "_attach_engine",
                                   return_value=(self.runtime.context, self.stop))
        attach.start(); self.addCleanup(attach.stop)
        self.s = Session(lease_factory=lambda **kw: self.lease)

    def test_start_names_the_mode_from_ownership(self):
        self.s.start()
        self.assertEqual(self.s.mode, "attached")
        self.assertTrue(self.s.alive())

    def test_alive_is_false_once_the_lease_is_gone(self):
        self.s.start(); self.lease.alive_flag = False
        self.assertFalse(self.s.alive())

    def test_each_session_gets_its_own_page_and_never_pages_zero(self):
        self.s.start()
        self.assertIs(self.s._page(), self.s._page())
        self.assertIsNot(self.s._page(), self.s._runtime.context.pages[0])

    def test_close_releases_the_lease_before_the_page(self):
        order = []
        self.s.start()
        self.s._lease.close = lambda: order.append("lease")
        self.s._page_.close = lambda: order.append("page")
        self.s.close()
        self.assertEqual(order[:2], ["lease", "page"])

    def test_terminate_asks_the_broker_to_stop(self):
        self.s.start()
        with mock.patch.object(browser.browser_broker, "shutdown") as down:
            self.s.terminate()
        down.assert_called_once_with(self.lease.cdpport, force=True)

    def test_reset_parks_only_its_own_page(self):
        self.s.start()
        own, first = self.s._page(), self.s._runtime.context.pages[0]
        self.s.reset()
        own.goto.assert_called_with("about:blank")
        first.goto.assert_not_called()

    def test_a_dead_engine_rebinds_and_retries_once(self):
        self.s.start()
        with mock.patch.object(browser, "_goto_allowing_download",
                               side_effect=[PlaywrightError("Target closed"), None]):
            self.s.page("https://example.org/a")
        self.assertEqual(browser._attach_engine.call_count, 2)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser -v`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'lease_factory'`

- [ ] **Step 3: 实现**

`_Runtime` 收缩为 `(context, stop, port)`。`_record` 由 broker 负责，`close()` 不再删发现文件。`FakePage.goto` 改成 `mock.Mock(side_effect=原实现)`（`assert_called_with`/`assert_not_called` 需要它是 Mock），原实现照旧弹 `queue`。`tests/test_browser.py` 既有 `SharedSessionTest`（真 Chrome）保留，`SessionTest` 的 `launcher=` 用例改为 `lease_factory=`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser tests.test_survey_run -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/browser.py tests/fake_browser.py tests/test_browser.py tests/test_survey_run.py
git commit -m "refactor(browser): Session is a lease client, one page per session"
```

---

### Task 7: 已登录态的首页预热

**Files:**
- Modify: `litcli/browser.py:633-665`（`page`）
- Test: `tests/test_browser.py`

**Interfaces:**
- Consumes: Task 6 的 `Session`；新增 `self._user_profile: bool`，在 `ensure()` 末尾由 `self._profile_source == "default"` 或 `self._profile == paths.user_browser_profile_dir()` 计算。
- Produces: `page()` 在挑战重试之后、**仍**是挑战页且 `self._user_profile` 时：`_goto(page, origin)` → `_settle` → 再 `_goto(page, url)`；origin 为 `urlparse(url)._replace(path="/", query="", fragment="")`。

- [ ] **Step 1: 写失败测试**

```python
def test_a_signed_in_profile_warms_site_root_before_giving_up(self):
    self.s._profile_source = "default"
    self.s.start()
    challenge = (403, "<html><title>Just a moment...</title></html>")
    self.runtime.page.queue = [challenge, challenge, (200, "<title>Journal</title>")]
    answer = self.s.page("https://www.sciencedirect.com/science/article/pii/S1")
    self.assertEqual(answer.status, 200)
    self.assertEqual(self.runtime.page.gotos,
                     ["https://www.sciencedirect.com/science/article/pii/S1"] * 2
                     + ["https://www.sciencedirect.com/",
                        "https://www.sciencedirect.com/science/article/pii/S1"])

def test_a_dedicated_profile_does_not_warm(self):
    self.s._profile_source = "dedicated"
    self.s.start()
    challenge = (403, "<html>Just a moment...</html>")
    self.runtime.page.queue = [challenge, challenge, challenge]
    self.s.page("https://www.sciencedirect.com/x")
    self.assertEqual(len(self.runtime.page.gotos), 3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser -v`
Expected: FAIL —— `gotos` 里没有站点根

- [ ] **Step 3: 实现**

按 Interfaces 加分支；`FakePage.__init__` 加 `self.gotos: list[str] = []`，`goto` 的原实现先 append 地址再弹 `queue`。`_user_profile` 在 `ensure()` 末尾算一次。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/browser.py tests/test_browser.py tests/fake_browser.py
git commit -m "feat(browser): warm the site root on a signed-in profile's challenge"
```

---

### Task 8: 命令面 `stop` / `status` / `login`

**Files:**
- Modify: `litcli/commands/parser.py:102-108`
- Modify: `litcli/cli.py:43-44`
- Modify: `litcli/commands/browser.py`（顶部 `from .. import browser_broker as broker`）
- Test: `tests/test_browser_cmd.py`

**Interfaces:**
- Consumes: `browser_broker.read_session / connect / shutdown / pin / ensure_lease`；`Session.mode`。
- Produces:
  - `cmd_browser_stop(cfg, args) -> int`：无会话 → 打 `no session` 退 0；有 → 读发现文件取 `owns`/`headless`/`pid`，`connect` 后 `shutdown(port, force=True)`、再 `lease.close()`，按去向打印并退 0：`owns` 为假 → `your Chrome was left running`；`owns` 且有头 → `left running (pid N) - close the window to end it`；`owns` 且无头 → `stopped (pid N)`
  - `cmd_browser_status`：先打 `profile` / `source` / `profile_directory` / `mode` / `leases` 头（`source` 取自发现文件同名键，验收 16 要看它）；结束只 `session.close()`
  - `cmd_browser_login`：`attached` → 只预热；`launched`+有头 → `pin` 后预热；`launched`+无头 → 最多 30 秒等 `leases` 清零，超时打 `someone is using the browser, try again later` 退 2；清零或无会话 → `shutdown(force=True)` 后 `Session(headless=False, pinned=True)` 并 `start()`

- [ ] **Step 1: 写失败测试**

```python
class StopTest(unittest.TestCase):
    def test_no_session_is_exit_0(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        self.assertIn("no session", " ".join(str(c.args[0]) for c in printed.call_args_list))

    def test_an_attached_browser_is_left_running(self):
        paths.browser_session_file().write_text(json.dumps(
            {"pid": 7, "control_port": 1, "cdp_port": 2, "owns": False, "headless": True}))
        with mock.patch.object(cmd.broker, "connect", return_value=FakeLease(owns=False)), \
                mock.patch.object(cmd.broker, "shutdown"), mock.patch("builtins.print") as printed:
            self.assertEqual(cmd.cmd_browser_stop({}, mock.Mock()), 0)
        self.assertIn("left running", " ".join(str(c.args[0]) for c in printed.call_args_list))

class LoginTest(unittest.TestCase):
    def test_an_attached_window_is_only_warmed(self):
        self.session.mode = "attached"
        with mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
            self.assertEqual(cmd.cmd_browser_login({}, mock.Mock(site="ieee")), 0)
        self.assertEqual(self.session.urls, [cmd.SITES["ieee"]])
        self.assertFalse(self.session.terminated)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser_cmd -v`
Expected: FAIL —— `AttributeError: module 'litcli.commands.browser' has no attribute 'cmd_browser_stop'`

- [ ] **Step 3: 实现**

parser 加 `bstop = bsub.add_parser("stop", help="stop the browser session this tool started")`；`_BROWSER_COMMANDS` 加 `"stop"`。`login` 的等待用 `status` 轮询 `leases`。`tests/fake_browser.py` 的 `FakeSession` 加 `self.mode = "launched"`，`tests/test_browser_cmd.py` 顶部从 `tests.fake_browser` 引入 `FakeLease`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_cmd -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/commands/parser.py litcli/cli.py litcli/commands/browser.py tests/test_browser_cmd.py
git commit -m "feat(browser): stop leaves the user's browser alone; status and login"
```

---

### Task 9: Windows 启动与锁

**Files:**
- Modify: `litcli/browser_broker.py`（`_spawn_broker`、`profile_lock`、`_is_headless`）
- Test: `tests/test_browser_broker.py`

**Interfaces:**
- Produces: `paths._IS_WINDOWS` 为真时 `_spawn_broker` 用 `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`、`close_fds=True`；`profile_lock` 在 Windows 走 `msvcrt.locking(fd, LK_NBLCK, 1)`，释放 `LK_UNLCK`；`_is_headless` 在无 `/proc` 时返回 `False`。

- [ ] **Step 1: 写失败测试**

```python
def test_windows_spawn_is_detached(self):
    with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
            mock.patch.object(bb.subprocess, "Popen") as popen:
        bb._spawn_broker(["x"])
    self.assertIn("creationflags", popen.call_args.kwargs)
    self.assertTrue(popen.call_args.kwargs["close_fds"])

def test_windows_lock_uses_locking_and_unlocks_on_exit(self):
    with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
            mock.patch.object(bb, "msvcrt") as lock:
        with bb.profile_lock(paths.browser_profile_dir()):
            pass
    self.assertEqual([c.args[1] for c in lock.locking.call_args_list],
                     [bb.msvcrt.LK_NBLCK, bb.msvcrt.LK_UNLCK])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: FAIL —— `creationflags` 不在 kwargs

- [ ] **Step 3: 实现**

两处都按 `paths._IS_WINDOWS` 分叉；`msvcrt` 用 `try: import msvcrt` / `except ImportError: msvcrt = None`，POSIX 上测试靠 `mock.patch.object(bb, "msvcrt")` 注入。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_browser_broker -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litcli/browser_broker.py tests/test_browser_broker.py
git commit -m "feat(browser): detached spawn and byte lock on Windows"
```

---

### Task 10: 文档、CHANGELOG 与验收脚本

**Files:**
- Modify: `README.md:226-231`、`README.en.md:272` 起同段
- Modify: `skills/litcli/SKILL.md` 的 `browser` 一节
- Modify: `CHANGELOG.md`（`[Unreleased] ### Changed` / `### Added`）
- Create: `scripts/check_browser_broker.sh`
- Test: `tests/test_docs.py`

**Interfaces:**
- Produces: 脚本对应设计验收 1–18，证据落 `$TMPDIR/litcli-broker-acceptance/`，`--only <n>` 跑单条，失败非 0 退出。

- [ ] **Step 1: 写失败测试**

```python
def test_both_readmes_name_the_new_profile_default(self):
    for name in ("README.md", "README.en.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        self.assertIn("browser stop", text)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_docs -v`
Expected: FAIL

- [ ] **Step 3: 写文档与脚本**

README 两份写：默认用你自己 Chrome 的 profile、`dedicated` 是什么、被占且没端口时的两条解法、`headless=true` 会让用户点图标启动的 Chrome 转发进隐藏实例、`browser stop` 的三种去向、以及“我们的写落在你真实会话上”。CHANGELOG 写清默认改变、迁移提示与 `stop`。脚本逐条实现验收 1–18 的前置与断言。

- [ ] **Step 4: 跑全量**

Run: `python3 -m unittest discover -s tests && ruff check litcli tests`
Expected: 全绿、ruff 无输出

- [ ] **Step 5: 提交**

```bash
git add README.md README.en.md skills/litcli/SKILL.md CHANGELOG.md scripts/check_browser_broker.sh tests/test_docs.py
git commit -m "docs(browser): the broker, the user's own profile and how to stop it"
```
