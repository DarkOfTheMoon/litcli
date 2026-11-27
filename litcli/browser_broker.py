"""The one owner of a browser per profile: it launches, holds and shuts it down.

Every command used to scan ``/proc`` and reap whatever else was on the profile, so two
commands at once killed each other's browser. The broker takes that over: the first command
that needs a browser gets one, everybody after it joins, and the last one to leave decides
when it goes. Clients keep the data path for themselves -- they attach over CDP to the port
the broker publishes -- so ``Session.page`` and ``Session.download`` are untouched.

A client holds a lease by keeping its ``hello`` connection open; EOF is the release, which
the kernel does even when the client is SIGKILLed. The broker is a supervisor, not a proxy.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import paths
from .errors import External

PROTOCOL_VERSION = 1

#: How long a freshly spawned browser gets to open its debugging port.
_READY_TIMEOUT = 20.0


@contextlib.contextmanager
def profile_lock(profile: Path, timeout: float = 2.0):
    """Exclusive lock on a profile: whatever is decided under it holds against a new client.

    A client cannot say ``hello`` before it holds this lock, so whoever leaves can trust a
    count of zero taken here. The lock file sits beside the profile, not inside it: Chrome
    owns everything inside ``--user-data-dir``.
    """
    profile = Path(profile)
    path = profile.parent / f"{profile.name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"profile is locked: {profile}") from None
                time.sleep(0.05)
        try:
            yield fd
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class BrowserUnavailable(External):
    """No usable browser: Playwright missing, the browser not downloaded, or nothing running."""

    exit_code = 2


class BrokerUnavailable(BrowserUnavailable):
    """No broker could be reached, started, or kept."""


class ProfileBusy(BrowserUnavailable):
    """The profile is held by a browser nobody can attach to; the user has to act."""

    exit_code = 1


class Backend(NamedTuple):
    """What the broker ends up owning: a CDP port, whether it started it, and its detach."""

    port: int
    owns: bool
    stop: Callable[[], None] | None
    pid: int | None = None


def cdp_ready(port: int, timeout: float = 0.5) -> bool:
    """Whether a browser answers CDP on ``port`` (the liveness probe for a detached session)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version",
                                    timeout=timeout) as answer:
            return answer.status == 200
    except (OSError, ValueError):
        return False


def _attach_engine(port: int):
    """A context of the already-running browser at ``port``, and the stop that detaches from it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "Playwright is not installed. Install it with `litcli backends install-browser` "
            "and retry (or fall back to a configured cookie + user_agent).") from e
    paths.apply_browser_cache()
    engine = sync_playwright().start()
    try:
        browser = engine.chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}")
    except Exception as e:
        engine.stop()
        raise BrowserUnavailable(
            f"could not attach to the running browser on port {port}: {e}") from e
    if not browser.contexts:
        engine.stop()
        raise BrowserUnavailable("the running browser has no window to work in")
    return browser.contexts[0], engine.stop


_BUSY_SENTENCE = (
    "your Chrome is already running on this profile without a debugging port - close it, "
    "or start it with --remote-debugging-port=0")

#: How a real Chrome is spelled on each platform (the ``chrome`` channel needs one installed).
_CHROME_BINARIES = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser")


class BrowserRuntime(NamedTuple):
    """A browser this process started: its port, its detach, its pid, and the attached context."""

    port: int
    stop: Callable[[], None] | None
    pid: int | None = None
    context: object | None = None


def _running_on(profile: Path, root: Path = Path("/proc")) -> list[tuple[int, int | None]]:
    """``(pid, debugging port)`` for every browser already started on this profile.

    Process rows only — a zygote or renderer carries the same command line and would otherwise
    be reaped on its own (it goes with the browser that forked it).

    Chrome keeps no list of itself, so this reads ``/proc``: a browser whose discovery file
    was lost (a crashed run, a cleaned ``/tmp``) is still there and still usable, and starting
    a second one per command is what fills the machine with them. Profiles carry across
    processes, so the ``user-data-dir`` argument is the whole match.

    The command line is read as a whole string, not split on NUL: Chrome rewrites its own
    argv into one space-joined line, so a per-argument match finds nothing at all.
    """
    marker = f"--user-data-dir={profile}"
    found = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return found  # not Linux: no /proc, and nothing is found (a launch follows)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            line = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue  # it exited under us
        at = line.find(marker)
        after = line[at + len(marker):at + len(marker) + 1] if at >= 0 else ""
        if at < 0 or after not in (" ", "\0", ""):
            continue  # absent, or a profile that only shares a prefix with ours
        if "--type=" in line:
            continue  # a zygote, renderer or utility process: it goes with its browser
        # Only the browser process takes a debugging port; its zygotes and renderers do not,
        # which is what picks the one that can be talked to out of the group.
        port = re.search(r"--remote-debugging-port=(\d+)", line)
        found.append((int(entry.name), int(port.group(1)) if port else None))
    return sorted(found)


def _wait_gone(pid: int, timeout: float = 3.0) -> None:
    """Wait for ``pid`` to disappear, escalating to SIGKILL.

    A browser takes a moment to shut down, and the next command (or a test's temp-dir cleanup)
    arrives immediately: without the wait it can still hold the profile lock, or still be
    writing into a directory about to be removed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def _is_headless(pid: int) -> bool:
    """Whether ``pid`` runs without a window (its command line says so)."""
    try:
        line = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return "--headless" in line


def _free_port() -> int:
    """A port free right now (Chrome fails to start on a taken one)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])

def _launch_detached(binary: str, profile, headless: bool, proxy: str | None,
                     profile_directory: str | None = None) -> BrowserRuntime:
    """Start a Chrome that outlives this process, then attach to it.

    Detached on purpose (``start_new_session``): `browser login` exits and the clearance must
    still be there for the next command, which attaches over CDP from a process of its own.
    """
    port = _free_port()
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    argv = [binary]
    if profile != paths.user_browser_profile_dir():
        # Chrome refuses remote debugging when --user-data-dir names the platform default data
        # directory, so the way to point at the user's own profile is to name it the same way
        # a hand-started Chrome does: not at all.
        argv.append(f"--user-data-dir={profile}")
    argv += [f"--remote-debugging-port={port}", "--no-first-run", "--no-default-browser-check",
             "about:blank"]
    if headless:
        argv.insert(1, "--headless=new")
    if profile_directory:
        argv.insert(1, f"--profile-directory={profile_directory}")
    if proxy:
        # The clearance is bound to the egress IP, so the browser must leave by the same proxy
        # as httpx or every cookie it earned is worthless to the request path.
        argv.insert(1, f"--proxy-server={proxy}")
    try:
        child = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    except OSError as e:
        raise BrowserUnavailable(f"could not start {binary}: {e}",
                                 hint="litcli backends install-browser") from e
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline and not cdp_ready(port):
        if child.poll() is not None:
            if child.returncode == 0:
                raise ProfileBusy(_BUSY_SENTENCE)
            raise BrowserUnavailable(
                f"{binary} exited at once (code {child.returncode}); a headed browser needs a "
                "display", hint="litcli backends install-browser")
        time.sleep(0.1)
    if not cdp_ready(port):
        raise BrowserUnavailable(
            f"{binary} never opened its debugging port (pid {child.pid})",
            hint="litcli backends install-browser")
    context, stop = _attach_engine(port)
    return BrowserRuntime(port, stop, child.pid, context)


def live_port(profile: Path) -> int | None:
    """The port a live browser on ``profile`` answers on, or ``None``.

    ``DevToolsActivePort`` can be stale (left from an earlier port-flagged run), so the file
    alone proves nothing: the port it names has to answer.
    """
    try:
        first = (Path(profile) / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
        port = int(first.strip())
    except (OSError, IndexError, ValueError):
        return None
    return port if cdp_ready(port, timeout=2.0) else None


def profile_holder(profile: Path) -> int | None:
    """The pid a running browser holds ``profile``'s singleton lock with, if any.

    The lock is a symlink named ``<hostname>-<pid>``. A lock from another machine names a pid
    that means nothing here, hence the hostname check.
    """
    try:
        target = os.readlink(Path(profile) / "SingletonLock")
    except OSError:
        return None
    host, _sep, pid = target.rpartition("-")
    if host != socket.gethostname() or not pid.isdigit():
        return None
    try:
        os.kill(int(pid), 0)
    except OSError:
        return None
    return int(pid)


def _bundled_binary() -> str:
    """The Chromium that ships with Playwright: the browser of last resort."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "Playwright is not installed. Install it with `litcli backends install-browser` "
            "and retry (or fall back to a configured cookie + user_agent).") from e
    paths.apply_browser_cache()
    engine = sync_playwright().start()
    try:
        return engine.chromium.executable_path
    finally:
        engine.stop()


def _binary(channel: str | None) -> str:
    """A Chrome to launch: the configured channel first, then any known name, then bundled."""
    for name in ((channel,) if channel else ()):
        if found := shutil.which(name):
            return found
    for name in _CHROME_BINARIES:
        if found := shutil.which(name):
            return found
    return _bundled_binary()


def _reap(profile: Path, keep: int | None) -> None:
    """Stop leftovers on ``profile``: headless ones anywhere, everything in our own dir.

    A headed browser on a profile the user may own is theirs to keep; only the dedicated
    directory has no other user's things in it, so there the old clean sweep still applies.
    """
    dedicated = Path(profile) == paths.browser_profile_dir()
    for pid, _port in _running_on(profile):
        if pid == keep:
            continue
        if not dedicated and not _is_headless(pid):
            continue
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def prepare(profile, headless: bool, profile_directory: str | None, channel: str | None,
            proxy: str | None) -> Backend:
    """Attach, report, or launch -- the three states, in that order of preference."""
    profile = Path(profile)
    port = live_port(profile)
    if port is not None:
        _context, stop = _attach_engine(port)
        return Backend(port, False, stop)
    holder = profile_holder(profile)
    if holder is not None:
        raise ProfileBusy(_BUSY_SENTENCE)
    _reap(profile, keep=None)
    rt = _launch_detached(_binary(channel), profile, headless, proxy, profile_directory)
    return Backend(rt.port, True, rt.stop, rt.pid)


class Broker:
    """Serves the control port over a socket only this profile's clients know about."""

    def __init__(self, *, profile: Path, headless: bool = False,
                 profile_directory: str | None = None, source: str = "default",
                 pinned: bool = False, idle_minutes: float = 10.0, backend=None,
                 clock=time.monotonic):
        self.profile = Path(profile)
        self.headless = headless
        self.profile_directory = profile_directory
        self.source = source
        self.pinned = pinned
        self.idle_minutes = idle_minutes
        self._backend = backend
        self._be: Backend | None = None
        self._clock = clock
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._selector = selectors.DefaultSelector()
        self._conns: set[socket.socket] = set()
        self._buffers: dict[socket.socket, bytes] = {}
        self._leases: set[socket.socket] = set()
        self._last_release = clock()
        self._stopping = False
        self._exited = False
        self.control_port = 0

    # -- control port ----------------------------------------------------------

    def bind(self, host: str, port: int) -> None:
        """Bind the control port; ``port=0`` picks a free one (tests bind before serving)."""
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen()
        self._listener = listener
        self.control_port = listener.getsockname()[1]

    @property
    def leases(self) -> int:
        return len(self._leases)

    @property
    def exited(self) -> bool:
        return self._exited

    def pin(self, pinned: bool = True) -> bool:
        """A pinned broker ignores the idle timer until something clears it or stops it."""
        self.pinned = bool(pinned)
        return self.pinned

    def tick(self) -> None:
        """One idle decision: exit when nobody has held a lease for ``idle_minutes``.

        The count is re-read under the lock so a client that is between "decided to connect"
        and "sent hello" is not cut off by the broker leaving underneath it.
        """
        if self._stopping or self.leases or self.pinned or self.idle_minutes <= 0:
            return
        if self._clock() - self._last_release < self.idle_minutes * 60:
            return
        try:
            with profile_lock(self.profile, timeout=2.0):
                if self.leases or self._stopping:
                    return
                self._leave()
        except TimeoutError:
            pass  # somebody is deciding right now; the next tick sees where it landed

    def _leave(self) -> None:
        self._stopping = True
        self._exited = True

    def stop(self, *, join: float | None = 5.0) -> None:
        """Leave the loop; idempotent, and safe from another thread.

        Joins the serving thread when called from outside it, so a caller that deletes the
        profile it served from does not race the teardown.
        """
        self._stopping = True
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(join)

    # -- lifetime --------------------------------------------------------------

    def serve(self) -> int:
        """Prepare the backend, publish it, and serve until asked to stop. Exit code."""
        if self._listener is None:
            self.bind("127.0.0.1", 0)
        assert self._listener is not None
        self._selector.register(self._listener, selectors.EVENT_READ, "listener")
        be = self._be = self._bring_up()
        if be is None:
            self._close_listener()
            self._exited = True
            return 1
        self._write_session(be)
        try:
            self._loop()
        finally:
            self._teardown(be)
        return 0

    def _bring_up(self) -> Backend | None:
        """Ask the injected backend for a browser; a busy profile becomes exit code 1."""
        if self._backend is None:
            from .browser_broker import prepare  # own module: keeps the name patchable

            self._backend = (lambda: prepare(self.profile, self.headless,
                                             self.profile_directory, None, None))
        try:
            return self._backend()
        except ProfileBusy as e:
            print(str(e), file=sys.stderr)
            return None

    def _loop(self) -> None:
        next_tick = time.monotonic() + 1.0
        while not self._stopping:
            for key, _mask in self._selector.select(0.2):
                if key.data == "listener":
                    self._accept()
                else:
                    self._readable(key.fileobj)
            now = time.monotonic()
            if now >= next_tick:
                next_tick = now + 1.0
                self.tick()

    def _accept(self) -> None:
        assert self._listener is not None
        conn, _peer = self._listener.accept()
        self._conns.add(conn)
        self._buffers[conn] = b""
        self._selector.register(conn, selectors.EVENT_READ, "client")

    def _readable(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(4096)
        except OSError:
            data = b""
        if not data:
            self._drop(conn)
            return
        buf = self._buffers.get(conn, b"") + data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                self._dispatch(conn, line)
                if conn not in self._conns:  # an error closed it
                    return
        self._buffers[conn] = buf

    def _drop(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            self._selector.unregister(conn)
        with contextlib.suppress(OSError):
            conn.close()
        self._conns.discard(conn)
        self._buffers.pop(conn, None)
        if conn in self._leases:
            self._leases.discard(conn)
            self._last_release = self._clock()

    def _dispatch(self, conn: socket.socket, line: bytes) -> None:
        try:
            msg = json.loads(line)
            cmd = msg["cmd"]
        except (ValueError, KeyError, TypeError):
            self._reply(conn, {"error": "one JSON object with a cmd per line"})
            self._drop(conn)
            return
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            self._reply(conn, {"error": f"unknown command: {cmd}"})
            self._drop(conn)
            return
        handler(conn, msg)
        if cmd != "hello" and conn not in self._leases and conn in self._conns:
            self._drop(conn)  # one command per connection -- except a lease, which keeps its own

    def _reply(self, conn: socket.socket, payload: dict) -> None:
        with contextlib.suppress(OSError):
            conn.sendall((json.dumps(payload) + "\n").encode())

    # -- commands --------------------------------------------------------------

    def _cmd_hello(self, conn: socket.socket, msg: dict) -> None:
        assert self._be is not None
        self._leases.add(conn)
        self._reply(conn, {"cdp_port": self._be.port, "broker_pid": os.getpid(),
                           "headless": self.headless, "pinned": self.pinned,
                           "owns": self._be.owns, "version": PROTOCOL_VERSION})

    def _cmd_status(self, conn: socket.socket, msg: dict) -> None:
        assert self._be is not None
        self._reply(conn, {"cdp_port": self._be.port, "pid": os.getpid(),
                           "headless": self.headless, "leases": self.leases,
                           "idle_s": 0.0 if self.leases
                           else self._clock() - self._last_release,
                           "version": PROTOCOL_VERSION, "profile": str(self.profile),
                           "profile_directory": self.profile_directory,
                           "source": self.source, "owns": self._be.owns})

    def _cmd_shutdown(self, conn: socket.socket, msg: dict) -> None:
        force = bool(msg.get("force"))
        try:
            with profile_lock(self.profile, timeout=2.0):
                stopped = force or not self.leases
        except TimeoutError:
            stopped = force  # a forced stop does not wait for a decidier that is busy
        self._reply(conn, {"ok": True, "stopped": stopped})
        if stopped:
            self._leave()

    def _cmd_pin(self, conn: socket.socket, msg: dict) -> None:
        self._reply(conn, {"ok": True, "pinned": self.pin(bool(msg.get("pinned", True)))})

    # -- publishing and teardown ----------------------------------------------

    def _session_fields(self, be: Backend) -> dict:
        return {"pid": os.getpid(), "control_port": self.control_port,
                "cdp_port": be.port, "profile": str(self.profile),
                "profile_directory": self.profile_directory, "source": self.source,
                "owns": be.owns, "version": PROTOCOL_VERSION, "headless": self.headless,
                "pinned": self.pinned, "started": time.time(), "leases": self.leases}

    def _write_session(self, be: Backend) -> None:
        # Renamed into place: a reader sees either no file or a whole one, never a torn line.
        path = paths.browser_session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._session_fields(be), fh)
        os.replace(tmp, path)

    def _teardown(self, be: Backend) -> None:
        for conn in list(self._conns):
            self._drop(conn)
        with contextlib.suppress(OSError):
            paths.browser_session_file().unlink()
        self._close_listener()
        if be.stop is not None:
            with contextlib.suppress(Exception):
                be.stop()
        if be.owns and self.headless and be.pid:
            # Only a headless browser we started is invisible enough to be nobody's to close.
            with contextlib.suppress(OSError):
                os.kill(be.pid, signal.SIGTERM)
            _wait_gone(be.pid)
        self._exited = True

    def _close_listener(self) -> None:
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._selector.unregister(self._listener)
            with contextlib.suppress(OSError):
                self._listener.close()
            self._listener = None


# -- client side ---------------------------------------------------------------


class Lease:
    """A held-open control connection: the release is its EOF, which the kernel does for us."""

    def __init__(self, sock: socket.socket, reader, control_port: int, cdp_port: int, owns: bool,
                 pinned: bool, version: int, headless: bool = False):
        self._sock: socket.socket | None = sock
        self._reader = reader
        self.control_port = control_port
        self.cdpport = cdp_port
        self.owns = owns
        self.pinned = pinned
        self.version = version
        self.headless = headless   # the broker's browser, not what this caller asked for

    def live(self) -> bool:
        """Whether the broker still answers on this lease's connection."""
        if self._sock is None:
            return False
        try:
            self._sock.sendall(b'{"cmd":"status"}\n')
            self._sock.settimeout(1.0)
            line = self._reader.readline()
            return bool(line) and isinstance(json.loads(line), dict)
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


def _conn(control_port: int, timeout: float = 2.0):
    try:
        sock = socket.create_connection(("127.0.0.1", int(control_port)), timeout=timeout)
    except OSError as e:
        raise BrokerUnavailable(f"no browser broker on port {control_port}: {e}") from e
    return sock, sock.makefile("rb")


def _exchange(control_port: int, msg: dict, timeout: float = 2.0) -> dict:
    sock, reader = _conn(control_port, timeout)
    try:
        sock.sendall((json.dumps(msg) + "\n").encode())
        return json.loads(reader.readline())
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def connect(control_port, *, version: int = PROTOCOL_VERSION, headless: bool,
            timeout: float = 2.0) -> Lease:
    """Take a lease on an existing broker; a busy or dead port raises ``BrokerUnavailable``."""
    sock, reader = _conn(control_port, timeout)
    try:
        sock.sendall((json.dumps({"cmd": "hello", "version": version,
                                  "headless": headless, "pid": os.getpid()}) + "\n").encode())
        answer = json.loads(reader.readline())
    except (OSError, ValueError) as e:
        with contextlib.suppress(OSError):
            sock.close()
        raise BrokerUnavailable(f"the broker on port {control_port} did not answer: {e}") from e
    return Lease(sock, reader, int(control_port), int(answer["cdp_port"]),
                 bool(answer.get("owns")), bool(answer.get("pinned")),
                 int(answer.get("version", PROTOCOL_VERSION)),
                 bool(answer.get("headless")))


def shutdown(control_port, *, force: bool = False) -> dict:
    return _exchange(control_port, {"cmd": "shutdown", "force": force})


def pin(control_port, *, pinned: bool = True) -> dict:
    return _exchange(control_port, {"cmd": "pin", "pinned": pinned})


def status(control_port, *, timeout: float = 2.0) -> dict:
    """One status snapshot of the broker on ``control_port``."""
    return _exchange(control_port, {"cmd": "status"}, timeout)


def read_session() -> dict | None:
    """The discovery file as a dict, or ``None`` when it is absent or unreadable."""
    try:
        info = json.loads(paths.browser_session_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("control_port") else None


def _spawn_broker(argv: list[str], spawn=subprocess.Popen):
    """Start a broker of its own, outliving this process (the same reason the browser does)."""
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "close_fds": True}
    if paths._IS_WINDOWS:
        kw["creationflags"] = (subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw["start_new_session"] = True
    return spawn(argv, **kw)


def _broker_argv(profile: Path, *, headless: bool, profile_directory, source,
                 idle_minutes: float, channel, proxy, pinned: bool) -> list[str]:
    argv = [sys.executable, "-m", "litcli.browser_broker", "--profile", str(profile),
            "--idle-minutes", str(idle_minutes), "--source", source]
    if profile_directory:
        argv += ["--profile-directory", profile_directory]
    if headless:
        argv.append("--headless")
    if channel:
        argv += ["--channel", channel]
    if proxy:
        argv += ["--proxy", proxy]
    if pinned:
        argv.append("--pinned")
    return argv


def _join(info: dict | None, version: int, headless: bool) -> Lease | None:
    if not info:
        return None
    try:
        return connect(info["control_port"], version=version, headless=headless)
    except BrokerUnavailable:
        return None


def _child_stderr(child) -> str:
    try:
        _out, err = child.communicate(timeout=2.0)
    except Exception:  # a mock, or a pipe that will not drain
        return ""
    return (err or b"").decode("utf-8", "replace").strip()


def ensure_lease(profile, *, headless: bool, profile_directory=None, source: str = "default",
                 idle_minutes: float = 10.0, channel=None, proxy=None, pinned: bool = False,
                 version: int = PROTOCOL_VERSION, wait: float = 20.0,
                 spawn=subprocess.Popen) -> Lease:
    """Join the broker on ``profile``, or start one. Everything below the lock re-reads.

    The lock is held across "read again, decide, spawn" so two clients cannot both decide the
    profile is free and both launch a browser into it.
    """
    profile = Path(profile)
    lease = _join(read_session(), version, headless)
    if lease is not None:
        return lease
    try:
        with profile_lock(profile, timeout=wait):
            lease = _join(read_session(), version, headless)
            if lease is not None:
                return lease
            child = _spawn_broker(_broker_argv(
                profile, headless=headless, profile_directory=profile_directory,
                source=source, idle_minutes=idle_minutes, channel=channel, proxy=proxy,
                pinned=pinned), spawn=spawn)
    except TimeoutError as e:
        raise BrokerUnavailable(f"the profile stayed locked: {profile}") from e
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        lease = _join(read_session(), version, headless)
        if lease is not None:
            return lease
        if child.poll() is not None:
            err = _child_stderr(child)
            if "without a debugging port" in err:
                raise ProfileBusy(err)
            raise BrokerUnavailable(err or f"the browser broker exited ({child.returncode})")
        time.sleep(0.05)
    raise BrokerUnavailable(
        f"the browser broker did not come up within {wait:g}s",
        hint="litcli browser status; LITCLI_DEBUG=1 litcli browser status shows the broker")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="litcli.browser_broker")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-directory")
    parser.add_argument("--source", default="default")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--channel")
    parser.add_argument("--proxy")
    parser.add_argument("--pinned", action="store_true")
    parser.add_argument("--idle-minutes", type=float, default=10.0)
    args = parser.parse_args(argv)
    broker = Broker(profile=Path(args.profile), headless=args.headless,
                    profile_directory=args.profile_directory, source=args.source,
                    pinned=args.pinned, idle_minutes=args.idle_minutes)
    return broker.serve()


if __name__ == "__main__":
    sys.exit(main())
