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
import json
import os
import selectors
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import paths
from .browser import BrowserUnavailable

PROTOCOL_VERSION = 1


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

    def tick(self) -> None:
        """One idle/shutdown decision. Task 2 fills it; the loop paces it once a second."""

    def stop(self) -> None:
        """Leave the loop; idempotent, and safe from another thread."""
        self._stopping = True

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
        if cmd != "hello" and conn in self._conns:
            self._drop(conn)

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
        if msg.get("force"):
            self._reply(conn, {"ok": True, "stopped": True})
            self.stop()
            return
        self._reply(conn, {"ok": True, "stopped": not self.leases})
        if not self.leases:
            self.stop()

    def _cmd_pin(self, conn: socket.socket, msg: dict) -> None:
        self.pinned = bool(msg.get("pinned", True))
        self._reply(conn, {"ok": True, "pinned": self.pinned})

    # -- publishing and teardown ----------------------------------------------

    def _session_fields(self, be: Backend) -> dict:
        return {"pid": os.getpid(), "control_port": self.control_port,
                "cdp_port": be.port, "profile": str(self.profile),
                "profile_directory": self.profile_directory, "source": self.source,
                "owns": be.owns, "version": PROTOCOL_VERSION, "headless": self.headless,
                "pinned": self.pinned, "started": time.time(), "leases": self.leases}

    def _write_session(self, be: Backend) -> None:
        path = paths.browser_session_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._session_fields(be), fh)

    def _teardown(self, be: Backend) -> None:
        for conn in list(self._conns):
            self._drop(conn)
        with contextlib.suppress(OSError):
            paths.browser_session_file().unlink()
        self._close_listener()
        if be.stop is not None:
            with contextlib.suppress(Exception):
                be.stop()
        self._exited = True

    def _close_listener(self) -> None:
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._selector.unregister(self._listener)
            with contextlib.suppress(OSError):
                self._listener.close()
            self._listener = None
