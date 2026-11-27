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
import http.client
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

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

PROTOCOL_VERSION = 1

#: The Windows spawn flags, spelled out because ``subprocess`` only defines them there.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

#: Where process command lines are read from; absent outside Linux.
_PROC = Path("/proc")

#: The kill that cannot be caught; Windows has only SIGTERM, which is the same thing there.
_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)

#: How long a freshly spawned browser gets to open its debugging port.
_READY_TIMEOUT = 20.0


def _lock(fd: int) -> None:
    if paths._IS_WINDOWS:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # one byte at the start, non-blocking
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if paths._IS_WINDOWS:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


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
                _lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"profile is locked: {profile}") from None
                time.sleep(0.05)
        try:
            yield fd
        finally:
            with contextlib.suppress(OSError):
                _unlock(fd)
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
    except (OSError, ValueError, http.client.HTTPException):
        # A port that answers something that is not HTTP is not a browser either: a broker's
        # control port answers one JSON line, which is a BadStatusLine here, not an OSError.
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


#: How long a browser that holds the profile is given to finish disappearing before the
#: profile is called somebody else's (a replacement arrives while the old browser is leaving).
_HOLD_SETTLE = 2.0
#: How long a heal waits for the profile lock, each of its two attempts.
_LOCK_WAIT = 5.0
_BUSY_SENTENCE = (
    "your Chrome is already running on this profile without a debugging port - close it, "
    "or start it with --remote-debugging-port=0")

#: The longest a heal can take: two attempts, each a lock wait and a full launch, plus the
#: settle between them.
_HEAL_BUDGET = 2 * (_READY_TIMEOUT + _LOCK_WAIT) + _HOLD_SETTLE
#: How long a hello (and a lease's own status check) waits for its answer. Both heal first,
#: and a heal may relaunch a browser.
_HELLO_TIMEOUT = _HEAL_BUDGET + 10.0

#: How a real Chrome is spelled on each platform (the ``chrome`` channel needs one installed).
_CHROME_BINARIES = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser")


class BrowserRuntime(NamedTuple):
    """A browser this process started: its port, its detach, its pid, and the attached context."""

    port: int
    stop: Callable[[], None] | None
    pid: int | None = None
    context: object | None = None


def _running_on(profile: Path, root: Path | None = None) -> list[tuple[int, int | None]]:
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
        entries = list((root or _PROC).iterdir())
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
        os.kill(pid, _KILL)


def _is_headless(pid: int) -> bool:
    """Whether ``pid`` runs without a window (its command line says so)."""
    try:
        line = (_PROC / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return "--headless" in line


def _port_file(profile: Path) -> Path:
    return Path(profile) / "DevToolsActivePort"


#: The line a browser prints once its debugging port is up.
_LISTENING = re.compile(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/")


def _stop_child(child) -> None:
    """Kill a browser whose port never came.

    Left alive it holds the profile with a lock and no ``DevToolsActivePort``: nothing can
    attach, the next launch reads a busy profile, and `browser stop` says "no session" because
    no session file was ever written.
    """
    with contextlib.suppress(OSError):
        os.kill(child.pid, _KILL)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        child.wait(5.0)


def _await_port(marker: Path, before: str | None, child, binary: str, start: float) -> int:
    """The port the new browser announced, or the one it put in its file.

    The announcement is the dependable source: only the first browser on a data directory
    writes ``DevToolsActivePort``, and the user's own directory is shared with whatever else
    they have open. The file is the fallback for a build that stays quiet, and only content
    that changed after the spawn counts (a dead run leaves its own behind).
    """
    announced: dict = {}

    def read_announcement() -> None:
        with contextlib.suppress(OSError, ValueError):
            for raw in child.stderr:  # a pipe, drained as it comes, so it cannot fill up
                found = _LISTENING.search(raw.decode("utf-8", "replace"))
                if found and "port" not in announced:
                    announced["port"] = int(found.group(1))

    threading.Thread(target=read_announcement, daemon=True).start()
    deadline = start + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if child.poll() is not None:
            if child.returncode == 0:
                raise ProfileBusy(_BUSY_SENTENCE)
            raise BrowserUnavailable(
                f"{binary} exited at once (code {child.returncode}); a headed browser needs a "
                "display", hint="litcli backends install-browser")
        if announced.get("port") and cdp_ready(announced["port"]):
            return announced["port"]
        try:
            after = marker.read_text(encoding="utf-8")
        except OSError:
            after = None
        if after and after != before:
            with contextlib.suppress(ValueError, IndexError):
                port = int(after.splitlines()[0])
                if cdp_ready(port):
                    return port
        time.sleep(0.1)
    raise BrowserUnavailable(
        f"{binary} never opened its debugging port (pid {child.pid})",
        hint="litcli backends install-browser")


def _launch_detached(binary: str, profile, headless: bool, proxy: str | None,
                     profile_directory: str | None = None, no_sandbox: bool = False) -> BrowserRuntime:
    """Start a Chrome that outlives this process, then attach to it.

    Detached on purpose (``start_new_session``): `browser login` exits and the clearance must
    still be there for the next command, which attaches over CDP from a process of its own.
    """
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    marker = _port_file(profile)
    try:
        before = marker.read_text(encoding="utf-8")
    except OSError:
        before = None
    # Always named: a headless Chrome left to pick its own directory works in a throwaway
    # scoped_dir with an empty cookie store, so nothing the user signed in would be read. The
    # name alone is not enough on the user's own profile, though -- see ``env`` below.
    argv = [binary, f"--user-data-dir={profile}"]
    # Port 0, and Chrome picks: it writes the port it got into DevToolsActivePort, but only
    # for an automatic port, so a port we pick would leave nothing behind for the next command
    # (or the one after a broker was killed) to find.
    argv += ["--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
             "about:blank"]
    if no_sandbox:
        argv.insert(1, "--no-sandbox")
    if headless:
        argv.insert(1, "--headless=new")
    if profile_directory:
        argv.insert(1, f"--profile-directory={profile_directory}")
    if proxy:
        # The clearance is bound to the egress IP, so the browser must leave by the same proxy
        # as httpx or every cookie it earned is worthless to the request path.
        argv.insert(1, f"--proxy-server={proxy}")
    env = None
    if paths.same_profile(profile, paths.user_browser_profile_dir()):
        # Naming the directory is not enough: Chrome opens no debugging port while the named
        # path is its platform default, and that default is derived from XDG_CONFIG_HOME. A
        # private config home for the child makes the named path non-default; the cookies are
        # in the directory either way.
        env = dict(os.environ)
        env.pop("XDG_DATA_HOME", None)
        private = paths.browser_private_xdg()
        private.mkdir(parents=True, exist_ok=True)
        env["XDG_CONFIG_HOME"] = str(private)
    start = time.monotonic()
    try:
        child = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, env=env)
    except OSError as e:
        raise BrowserUnavailable(f"could not start {binary}: {e}",
                                 hint="litcli backends install-browser") from e
    try:
        port = _await_port(marker, before, child, binary, start)
    except (BrowserUnavailable, ProfileBusy):
        _stop_child(child)
        raise
    try:
        known = marker.read_text(encoding="utf-8").splitlines()[:1]
    except OSError:
        known = []
    if known != [str(port)]:  # the next command looks here, and the file may hold another's
        with contextlib.suppress(OSError):
            marker.write_text(f"{port}\n", encoding="utf-8")
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


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` exists, without disturbing it.

    ``os.kill(pid, 0)`` is ``TerminateProcess`` on Windows, so it cannot be used as a probe
    there: the query-and-close below is read-only. Elsewhere signal 0 does nothing at all.
    """
    if not paths._IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except OSError:
        return False


def profile_holder(profile: Path) -> int | None:
    """The pid a running browser holds ``profile``'s singleton lock with, if any.

    The lock is a symlink named ``<hostname>-<pid>``. A lock from another machine names a pid
    that means nothing here, hence the hostname check.
    """
    lock = Path(profile) / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        # Windows writes the lock as a plain file (a symlink needs privilege there); the text
        # carries the same ``<hostname>-<pid>``.
        try:
            target = lock.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
    host, _sep, pid = target.rpartition("-")
    if host != socket.gethostname() or not pid.isdigit():
        return None
    # A crashed browser leaves its lock behind and nobody cleans it up, so the pid has to be
    # checked: a stale lock read as a holder blocks every launch until the number is reused.
    return int(pid) if _pid_alive(int(pid)) else None


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


def _binary(channel: str | None) -> tuple[str, bool]:
    """A Chrome to launch and whether it needs ``--no-sandbox``: channel, known name, bundled.

    Playwright's Chromium ships ``chrome_sandbox`` not setuid, so on a machine that also
    restricts unprivileged user namespaces it aborts at once with "No usable sandbox".
    """
    for name in ((channel,) if channel else ()):
        if found := shutil.which(name):
            return found, False
    for name in _CHROME_BINARIES:
        if found := shutil.which(name):
            return found, False
    return _bundled_binary(), True


def _reap(profile: Path, keep: int | None) -> None:
    """Stop leftovers on ``profile``: headless ones anywhere, everything in our own dir.

    A headed browser on a profile the user may own is theirs to keep; only the dedicated
    directory has no other user's things in it, so there the old clean sweep still applies.
    """
    dedicated = paths.same_profile(profile, paths.browser_profile_dir())
    found = _running_on(profile)
    if not found and not _PROC.is_dir():
        # No /proc: the lock is the only place a leftover can be found there.
        pid = profile_holder(profile)
        found = [(pid, None)] if pid is not None else []
    for pid, _port in found:
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
    deadline = time.monotonic() + _HOLD_SETTLE
    while True:
        port = live_port(profile)
        if port is not None:
            _context, stop = _attach_engine(port)
            return Backend(port, False, stop)
        if profile_holder(profile) is None:
            break
        if time.monotonic() >= deadline:
            raise ProfileBusy(_BUSY_SENTENCE)
        time.sleep(0.05)
    _reap(profile, keep=None)
    binary, no_sandbox = _binary(channel)
    rt = _launch_detached(binary, profile, headless, proxy, profile_directory,
                          no_sandbox=no_sandbox)
    return Backend(rt.port, True, rt.stop, rt.pid)


class Broker:
    """Serves the control port over a socket only this profile's clients know about."""

    def __init__(self, *, profile: Path, headless: bool = False,
                 profile_directory: str | None = None, source: str = "default",
                 pinned: bool = False, idle_minutes: float = 10.0, channel: str | None = None,
                 proxy: str | None = None, backend=None, clock=time.monotonic):
        self.profile = Path(profile)
        # The user's own profile is their Chrome's home: a visible window of ours there would
        # shadow it, so the shared browser on it is always hidden whatever the caller asked.
        # `browser login` is the one place a window is due, and it starts a browser of its own
        # instead of a broker. Everywhere else the caller's choice stands.
        self.headless = headless or paths.same_profile(
            self.profile, paths.user_browser_profile_dir())
        self.profile_directory = profile_directory
        self.source = source
        self.channel = channel
        self.proxy = proxy
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
        self._torn_down = False
        self._exited = False
        self._last_busy: str | None = None
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
        with self._locked() as locked:
            if not locked:
                return  # somebody is deciding right now; the next tick sees where it landed
            if self.leases or self._stopping:
                return
            self._leave()
            # The teardown stays under this lock: a client arriving now must not read the
            # file, join, and be handed the browser that is being killed this second.
            self._teardown_locked()

    @contextlib.contextmanager
    def _locked(self, timeout: float = 2.0):
        """``profile_lock`` as a context, yielding whether it was actually taken.

        A lock that cannot be had is not an error here: the caller's clients take it again a
        moment later and re-read the world, so giving up is safe.
        """
        try:
            with profile_lock(self.profile, timeout=timeout):
                yield True
        except TimeoutError:
            yield False

    def _leave(self) -> None:
        self._stopping = True
        self._exited = True

    def _teardown_locked(self) -> None:
        if self._torn_down:
            return
        self._teardown()
        self._torn_down = True

    def _finish(self) -> None:
        """Tear down for good, holding the profile lock while the browser goes."""
        with self._locked():
            self._teardown_locked()

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
        try:
            be = self._be = self._bring_up()
        except BrowserUnavailable as e:
            # Nobody reads this process's stderr directly -- it is a pipe read by the spawning
            # client -- but an escaping exception would print nothing at all: no line and a
            # traceback on an unread pipe. The sentence and the code are what the client shows.
            print(str(e), file=sys.stderr)
            self._close_listener()
            self._exited = True
            return e.exit_code
        if be is None:
            self._close_listener()
            self._exited = True
            return 1
        self._write_session(be)
        try:
            self._loop()
        finally:
            self._finish()
        return 0

    def _bring_up(self, quiet: bool = False) -> Backend | None:
        """Ask the injected backend for a browser; a busy profile becomes exit code 1.

        ``quiet`` keeps the busy sentence off stderr for a first attempt that will be retried.
        A busy profile leaves its sentence in ``_last_busy``: ``None`` here means "no browser",
        but the caller's reply should still say what the user has to do about it.
        """
        self._last_busy = None
        if self._backend is None:
            from .browser_broker import prepare  # own module: keeps the name patchable

            self._backend = (lambda: prepare(self.profile, self.headless,
                                             self.profile_directory, self.channel,
                                             self.proxy))
        try:
            return self._backend()
        except ProfileBusy as e:
            self._last_busy = str(e)
            if not quiet:
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

    def _heal(self) -> str | None:
        """Replace a backend whose browser is gone, before a client is told its port.

        An attached browser dies whenever the user closes its window or Chrome crashes; the
        broker would otherwise keep handing out the stale port and every later command would
        fail until it idled out. A browser of our own is checked the same way -- a crashed one
        leaves exactly the same corpse. Returns why it could not, or ``None`` when it healed
        (or had nothing to heal).
        """
        be = self._be
        if be is None or cdp_ready(be.port, timeout=0.5):
            return None
        if cdp_ready(be.port, timeout=2.0):
            return None  # a busy browser, not a dead one: a page loading can outlast a probe
        with contextlib.suppress(Exception):
            if be.stop is not None:
                be.stop()
        new, failure, spoken = None, None, False
        for attempt in range(2):
            try:
                with profile_lock(self.profile, timeout=_LOCK_WAIT):
                    new = self._bring_up(quiet=attempt == 0)
            except (BrowserUnavailable, TimeoutError) as e:
                failure, spoken = e, False
            else:
                failure = self._last_busy
                # The attempt itself says a busy sentence when it is not the quiet first one.
                spoken = failure is not None and attempt > 0
            if new is not None:
                break
            if attempt == 0:
                # A hold that clears itself (the old browser still letting go, or the user
                # opening Chrome right now) is not worth the broker every client is on.
                time.sleep(_HOLD_SETTLE)
        if new is None:
            # Either way this broker has no browser it can serve, so it leaves and the next
            # client starts a fresh one. The reply carries the reason: the stderr pipe of a
            # detached broker has no reader. A sentence the attempt already printed is not
            # printed a second time here; a failed relaunch has not been said yet.
            reason = str(failure) if failure else "the browser is gone"
            if not spoken:
                print(reason, file=sys.stderr)
            self._leave()
            return reason
        self._be = new
        self._write_session(new)
        return None

    def _cmd_hello(self, conn: socket.socket, msg: dict) -> None:
        # A client that is about to stop the broker asks not to be healed: the relaunch would
        # only be undone a moment later, and the stale backend holds the pid stop reports.
        reason = self._heal() if msg.get("heal", True) else None
        if reason is not None:
            self._reply(conn, {"error": reason})
            return
        assert self._be is not None
        self._leases.add(conn)
        self._reply(conn, {"cdp_port": self._be.port, "broker_pid": os.getpid(),
                           "browser_pid": self._be.pid,
                           "headless": self.headless, "pinned": self.pinned,
                           "owns": self._be.owns, "version": PROTOCOL_VERSION})

    def _cmd_status(self, conn: socket.socket, msg: dict) -> None:
        reason = self._heal()
        if reason is not None:
            self._reply(conn, {"error": reason})
            return
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
        with self._locked() as locked:
            # A forced stop does not wait for a decidier that is busy.
            if not (force or locked) or (not force and self.leases):
                self._reply(conn, {"ok": True, "stopped": False})
                return
            # The reply goes first: the teardown drops this connection.
            self._reply(conn, {"ok": True, "stopped": True})
            self._leave()
            self._teardown_locked()

    def _cmd_pin(self, conn: socket.socket, msg: dict) -> None:
        self._reply(conn, {"ok": True, "pinned": self.pin(bool(msg.get("pinned", True)))})

    # -- publishing and teardown ----------------------------------------------

    def _session_fields(self, be: Backend) -> dict:
        return {"pid": os.getpid(), "control_port": self.control_port,
                "cdp_port": be.port, "browser_pid": be.pid, "profile": str(self.profile),
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

    def _teardown(self) -> None:
        # ``self._be``, not the one ``serve`` started with: a backend replaced by ``_heal``
        # is the live one, and the old one was detached when it was replaced.
        be = self._be
        for conn in list(self._conns):
            self._drop(conn)
        if be is not None:
            if be.stop is not None:
                with contextlib.suppress(Exception):
                    be.stop()
            if be.owns and self.headless and be.pid:
                # Only a headless browser we started is invisible enough to be nobody's to close.
                with contextlib.suppress(OSError):
                    os.kill(be.pid, _KILL)
                with contextlib.suppress(OSError):
                    _wait_gone(be.pid)
        # The browser is gone now, so the file may follow it: a reader that finds it still
        # gets a broker whose browser answers. It is only removed while it is still ours.
        info = read_session()
        if info is None or info.get("control_port") == self.control_port:
            with contextlib.suppress(OSError):
                paths.browser_session_file().unlink()
        self._close_listener()
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
                 pinned: bool, version: int, headless: bool = False,
                 browser_pid: int | None = None):
        self._sock: socket.socket | None = sock
        self._reader = reader
        self.control_port = control_port
        self.cdpport = cdp_port
        self.owns = owns
        self.pinned = pinned
        self.version = version
        self.headless = headless   # the broker's browser, not what this caller asked for
        self.browser_pid = browser_pid  # the window, when there is one; gone with stop

    def live(self) -> bool:
        """Whether the broker still answers on this lease's connection."""
        if self._sock is None:
            return False
        try:
            self._sock.sendall(b'{"cmd":"status"}\n')
            # The broker serves one connection at a time and ``status`` heals first: the wait
            # is the heal's budget, or a healthy broker reads as dead for the whole of it.
            self._sock.settimeout(_HELLO_TIMEOUT)
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


def _error_of(answer: dict) -> str | None:
    """The broker's reason out of a reply, when it answered with one instead of data."""
    error = answer.get("error")
    return error if isinstance(error, str) and error else None


def _exchange(control_port: int, msg: dict, timeout: float = 2.0) -> dict:
    sock, reader = _conn(control_port, timeout)
    try:
        sock.sendall((json.dumps(msg) + "\n").encode())
        answer = json.loads(reader.readline())
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    error = _error_of(answer) if isinstance(answer, dict) else None
    if error is not None:
        raise BrokerUnavailable(error)
    return answer


def connect(control_port, *, version: int = PROTOCOL_VERSION, headless: bool,
            timeout: float = 2.0, heal: bool = True) -> Lease:
    """Take a lease on an existing broker; a busy or dead port raises ``BrokerUnavailable``.

    ``heal`` is what a caller that means to keep the session wants. ``browser stop`` turns it
    off: a heal there relaunches a browser the user has just closed, and the stop would take
    that new one down again -- a window flashing up in the middle of a stop.
    """
    sock, reader = _conn(control_port, timeout)
    try:
        # The broker heals before answering a hello, and a heal may relaunch the browser: as
        # long a wait as a cold launch, not the 2 s a broker with nothing to do needs.
        sock.settimeout(_HELLO_TIMEOUT)
        sock.sendall((json.dumps({"cmd": "hello", "version": version, "heal": heal,
                                  "headless": headless, "pid": os.getpid()}) + "\n").encode())
        answer = json.loads(reader.readline())
    except (OSError, ValueError) as e:
        with contextlib.suppress(OSError):
            sock.close()
        raise BrokerUnavailable(f"the broker on port {control_port} did not answer: {e}") from e
    error = _error_of(answer) if isinstance(answer, dict) else None
    if error is not None:
        with contextlib.suppress(OSError):
            sock.close()
        raise BrokerUnavailable(error)
    return Lease(sock, reader, int(control_port), int(answer["cdp_port"]),
                 bool(answer.get("owns")), bool(answer.get("pinned")),
                 int(answer.get("version", PROTOCOL_VERSION)),
                 bool(answer.get("headless")),
                 answer.get("browser_pid"))


def shutdown(control_port, *, force: bool = False) -> dict:
    return _exchange(control_port, {"cmd": "shutdown", "force": force})


def pin(control_port, *, pinned: bool = True) -> dict:
    return _exchange(control_port, {"cmd": "pin", "pinned": pinned})


def status(control_port, *, timeout: float = _HELLO_TIMEOUT) -> dict:
    """One status snapshot of the broker on ``control_port``.

    The default is the heal budget: the broker heals before it answers, and a caller that gave
    up sooner reads a healing broker as absent -- `_others_on` would then replace it under its
    own clients.
    """
    return _exchange(control_port, {"cmd": "status"}, timeout)


def read_session() -> dict | None:
    """The discovery file as a dict, or ``None`` when it is absent or unreadable."""
    try:
        info = json.loads(paths.browser_session_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("control_port") else None


def _spawn_broker(argv: list[str], spawn=None):
    """Start a broker of its own, outliving this process (the same reason the browser does)."""
    spawn = spawn or subprocess.Popen   # looked up late, so a test can stand in for it
    env = dict(os.environ)
    # The child is `python -m litcli.browser_broker` and its search path starts at the cwd, but
    # the command that spawns it runs from wherever the user stands. The package root (in an
    # install, site-packages; from source, the repo) makes the import independent of both.
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [root, env.get("PYTHONPATH")]))
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "close_fds": True,
                "env": env}
    if paths._IS_WINDOWS:
        kw["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
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


def _same_profile(spelled, profile) -> bool:
    """Whether a discovery file's profile is ``profile``, however each is spelled.

    ``/p/``, ``/p/.`` and a symlinked ``/p`` are one directory; a string compare spends a
    healthy broker (and the browser it holds) on a spelling.
    """
    if spelled in (None, ""):
        return True  # older files carry no field; guessing "different" replaces needlessly
    return paths.same_profile(spelled, profile)


def _join(info: dict | None, version: int, headless: bool,
          profile: Path | None = None) -> Lease | None:
    """A lease on the broker ``info`` names -- unless it serves another profile.

    ``[browser].profile`` can be edited while a broker runs. Joining it anyway would hand back
    the old profile's port and the new setting would silently do nothing.
    """
    if not info:
        return None
    if profile is not None and not _same_profile(info.get("profile"), profile):
        return None
    try:
        return connect(info["control_port"], version=version, headless=headless)
    except BrokerUnavailable:
        return None


def _await_retired(control_port: int, timeout: float = 10.0) -> None:
    """Wait for a broker that was told to leave, and the browser it goes down with.

    Its teardown removes the discovery file only once the browser is gone, so the file
    disappearing (or changing hands) is what says the profile is free again.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read_session()
        if info is None or info.get("control_port") != control_port:
            return
        time.sleep(0.05)


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
    lease = _join(read_session(), version, headless, profile)
    if lease is not None:
        return lease
    try:
        with profile_lock(profile, timeout=wait):
            info = read_session()
            lease = _join(info, version, headless, profile)
            if lease is not None:
                return lease
            if info and not _same_profile(info.get("profile"), profile):
                # It serves another profile: stop it (nobody there can use it for this one)
                # and wait for its browser before a new broker comes up on ours.
                with contextlib.suppress(BrokerUnavailable, OSError):
                    shutdown(info["control_port"], force=True)
                _await_retired(info["control_port"])
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
                    pinned=args.pinned, idle_minutes=args.idle_minutes,
                    channel=args.channel, proxy=args.proxy)
    return broker.serve()


if __name__ == "__main__":
    # `-m` makes this file __main__: a second module with a second copy of every class, while
    # the relative imports inside it hand back the package's copies. Every `isinstance` and
    # `except` across that seam compares two different classes and quietly misses. Entering
    # through the package's own module keeps one copy of each.
    from .browser_broker import main as package_main

    sys.exit(package_main())
