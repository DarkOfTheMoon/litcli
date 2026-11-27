"""The broker's control port: one line per command, EOF is a released lease."""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from litcli import browser_broker as bb
from litcli import paths


def _serve(backend=lambda: bb.Backend(41_000, True, None), **kw):
    """Run a broker in a thread; return (broker, control_port)."""
    b = bb.Broker(profile=paths.browser_profile_dir(), **kw, backend=backend)
    b.bind("127.0.0.1", 0)
    b._thread = threading.Thread(target=b.serve, daemon=True)
    b._thread.start()
    for _ in range(100):
        info = bb.read_session()
        if info and info["control_port"] == b.control_port:
            break
        time.sleep(0.02)
    return b, b.control_port


def _line(port, msg, timeout=bb._HELLO_TIMEOUT):
    # The client wait is the implementation's own: a hello heals first, and the first
    # `urlopen` of a process pays the TLS-context cost (on Windows it walks the certificate
    # store, which alone outlasts a couple of seconds).
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall((json.dumps(msg) + "\n").encode())
        return json.loads(s.makefile().readline())


def _hold(port, version=bb.PROTOCOL_VERSION):
    """Open a hello connection and keep it (one lease); the caller closes it."""
    s = socket.create_connection(("127.0.0.1", port), timeout=bb._HELLO_TIMEOUT)
    s.sendall((json.dumps({"cmd": "hello", "version": version,
                           "headless": True, "pid": 1}) + "\n").encode())
    s.makefile().readline()
    return s


def _backend_from(*items):
    """A backend giving back the listed backends and raising the listed exceptions, in order."""
    answers = iter(items)

    def backend():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return backend


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self._home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_hello_names_the_browser_pid_beside_the_broker_pid(self):
        # "close the window to end it" has to name the window: the broker's own pid goes with
        # the broker, and the window outlives it.
        b, port = _serve(backend=lambda: bb.Backend(41_000, True, None, 4242))
        try:
            answer = _line(port, {"cmd": "hello", "version": bb.PROTOCOL_VERSION,
                                  "headless": False, "pid": 1})
            self.assertEqual(answer["browser_pid"], 4242)
            self.assertEqual(answer["broker_pid"], os.getpid())
        finally:
            b.stop()

    def test_connect_carries_the_browser_pid_to_the_lease(self):
        # `browser stop` prints this pid. Without it the message falls back to the broker's own,
        # which dies with the stop, so the reader is handed a pid that is already gone.
        b, port = _serve(backend=lambda: bb.Backend(41_000, True, None, 4242))
        try:
            lease = bb.connect(port, headless=False)
            try:
                self.assertEqual(lease.browser_pid, 4242)
            finally:
                lease.close()
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

    def test_a_dead_attached_browser_is_replaced_before_the_next_lease_is_answered(self):
        # The user closed the window this broker had attached to. Nothing else notices, so
        # without a look here every later command binds the stale port and fails, and the
        # broker keeps serving the corpse until it idles out.
        stops = []
        answers = iter([bb.Backend(41_001, False, lambda: stops.append("old")),
                        bb.Backend(41_002, True, lambda: stops.append("new"), 777)])
        with mock.patch.object(bb, "cdp_ready",
                               side_effect=lambda port, timeout=0.5: port == 41_002):
            b, port = _serve(backend=lambda: next(answers), idle_minutes=0)
            try:
                answer = _line(port, {"cmd": "hello", "version": bb.PROTOCOL_VERSION,
                                      "headless": True, "pid": 1})
                live = bb.read_session()["cdp_port"]
            finally:
                b.stop()
        self.assertEqual(answer["cdp_port"], 41_002)
        self.assertTrue(answer["owns"])
        # The dead attach is detached; the new backend's own stop runs at the broker's teardown.
        self.assertEqual(stops, ["old", "new"])
        self.assertEqual(live, 41_002)

    def test_teardown_stops_the_browser_before_the_discovery_file_goes(self):
        # The file is what a client follows. Gone first, the client starts a new broker which
        # finds the old browser still answering and attaches to it -- and the old one is killed
        # a moment later, leaving the new broker on a corpse.
        seen = []

        def stop():
            seen.append(bb.read_session() is not None)

        b, _port = _serve(backend=lambda: bb.Backend(41_000, True, stop), idle_minutes=0)
        b.stop()
        self.assertEqual(seen, [True])
        self.assertIsNone(bb.read_session())

    def test_teardown_holds_the_profile_lock_while_the_browser_goes(self):
        # A client's "read the file, take the lock, start if gone" must not run mid-teardown.
        held = []

        def stop():
            def probe():
                try:
                    with bb.profile_lock(paths.browser_profile_dir(), timeout=0.2):
                        held.append("free")
                except TimeoutError:
                    held.append("locked")
            t = threading.Thread(target=probe)
            t.start()
            t.join()

        b, _port = _serve(backend=lambda: bb.Backend(41_000, True, stop), idle_minutes=0)
        b.stop()
        self.assertEqual(held, ["locked"])

    def test_status_also_looks_at_a_browser_that_died(self):
        answers = iter([bb.Backend(41_001, False, None), bb.Backend(41_002, True, None, 7)])
        with mock.patch.object(bb, "cdp_ready",
                               side_effect=lambda port, timeout=0.5: port == 41_002):
            b, port = _serve(backend=lambda: next(answers), idle_minutes=0)
            try:
                self.assertEqual(_line(port, {"cmd": "status"})["cdp_port"], 41_002)
            finally:
                b.stop()

    def test_status_names_the_profile_directory(self):
        b, port = _serve(profile_directory="Profile 1")
        try:
            self.assertEqual(_line(port, {"cmd": "status"})["profile_directory"], "Profile 1")
        finally:
            b.stop()

    def test_a_browser_that_cannot_start_is_a_message_not_a_traceback(self):
        # The broker is spawned detached, so its stderr is a pipe nobody reads: an exception
        # escaping ``main`` prints nothing and the client's only clue is an empty pipe. The
        # exit code and the sentence are what the client turns into the user's error.
        def boom():
            raise bb.BrowserUnavailable("no display here for a window")

        b = bb.Broker(profile=paths.browser_profile_dir(), backend=boom)
        b.bind("127.0.0.1", 0)
        err = io.StringIO()
        with mock.patch.object(bb.sys, "stderr", err):
            self.assertEqual(b.serve(), 2)
        self.assertIn("no display here for a window", err.getvalue())
        self.assertTrue(b.exited)

    def test_a_slow_answer_is_not_a_dead_browser(self):
        # A busy browser can take longer than half a second to answer /json/version without
        # being gone (a page loading, a renderer stalled). One short probe would replace a
        # perfectly good browser, and a replacement on the user's own profile means a relaunch.
        calls = []

        def ready(port, timeout=0.5):
            calls.append(timeout)
            return len(calls) > 1

        b, port = _serve(backend=lambda: bb.Backend(41_001, False, None), idle_minutes=0)
        with mock.patch.object(bb, "cdp_ready", side_effect=ready):
            try:
                self.assertEqual(_line(port, {"cmd": "status"})["cdp_port"], 41_001)
            finally:
                b.stop()
        self.assertEqual(calls, [0.5, 2.0])  # the second look waits longer

    def test_a_temporarily_held_profile_gets_a_second_try_before_the_broker_leaves(self):
        # The old browser is still letting go of the lock, or the user is opening Chrome this
        # very second. Leaving on the first refusal kills the broker every in-flight client is
        # attached to, over a hold that clears a moment later.
        backend = _backend_from(bb.Backend(41_001, True, None),     # up, then dies
                                bb.ProfileBusy(bb._BUSY_SENTENCE),  # the name is briefly taken
                                bb.Backend(41_002, True, None, 9))
        with mock.patch.object(bb.sys, "stderr", io.StringIO()):
            b, port = _serve(backend=backend, idle_minutes=0)
        with mock.patch.object(bb, "cdp_ready",
                               side_effect=lambda port, timeout=0.5: port == 41_002), \
                mock.patch.object(bb, "_HOLD_SETTLE", 0.05), \
                mock.patch.object(bb.sys, "stderr", io.StringIO()):
            try:
                self.assertEqual(_line(port, {"cmd": "status"})["cdp_port"], 41_002)
                self.assertEqual(bb.read_session()["cdp_port"], 41_002)
            finally:
                b.stop()

    def test_a_failed_relaunch_says_why_before_the_broker_leaves(self):
        # Otherwise the client is left with "did not answer" and knows nothing about the cause,
        # which is the same silence the traceback gave, only shorter.
        backend = _backend_from(bb.Backend(41_001, True, None),
                                bb.BrowserUnavailable("no usable browser here"),
                                bb.BrowserUnavailable("no usable browser here"))
        b, port = _serve(backend=backend, idle_minutes=0)
        err = io.StringIO()
        with mock.patch.object(bb, "cdp_ready", return_value=False), \
                mock.patch.object(bb, "_HOLD_SETTLE", 0.05), \
                mock.patch.object(bb.sys, "stderr", err):
            try:
                _line(port, {"cmd": "status"})
            finally:
                b.stop()
        self.assertIn("no usable browser here", err.getvalue())
        self.assertEqual(err.getvalue().count("no usable browser here"), 1)

    def test_teardown_kills_only_a_headless_browser_it_started(self):
        # `browser stop`'s contract: our own headless browser goes, a window of ours is left
        # for the user to close, and a browser we merely attached to is never touched.
        cases = [(True, True, True), (True, False, False), (False, True, False)]
        for owns, headless, killed in cases:
            with self.subTest(owns=owns, headless=headless):
                b, _port = _serve(
                    backend=lambda owns=owns: bb.Backend(41_000, owns, None, 4242),
                    headless=headless, idle_minutes=0)
                with mock.patch.object(bb.os, "kill") as kill, \
                        mock.patch.object(bb, "_wait_gone"):
                    b.stop()
                self.assertEqual(kill.called, killed)
                if killed:
                    kill.assert_called_once_with(4242, bb._KILL)

    def test_the_hello_waits_out_a_heal(self):
        # ``hello`` heals first, and a heal may relaunch the browser: that takes as long as a
        # cold launch, far longer than the 2 s that is enough for a broker that has nothing to
        # do. The read timeout for the hello is the heal budget, not a guess.
        sock = mock.Mock()
        reader = mock.Mock()
        reader.readline.return_value = json.dumps({"cdp_port": 41_000}).encode()
        with mock.patch.object(bb, "_conn", return_value=(sock, reader)):
            lease = bb.connect(41999, headless=True)
        lease.close()
        timeouts = [c.args[0] for c in sock.settimeout.call_args_list]
        self.assertTrue(any(t >= bb._READY_TIMEOUT for t in timeouts), timeouts)

    def test_the_hello_budget_covers_the_worst_case_heal(self):
        # Two attempts, each a lock wait plus a full launch, plus the settle between them: a
        # hello that gave up sooner would time out a heal that is still within its own budget.
        worst = 2 * (bb._READY_TIMEOUT + bb._LOCK_WAIT) + bb._HOLD_SETTLE
        self.assertGreaterEqual(bb._HELLO_TIMEOUT, worst)

    def test_a_lease_status_waits_out_a_heal(self):
        # ``status`` heals too, and the broker serves one connection at a time: during a heal
        # this lease's own check waits as long as possible, otherwise a healthy broker reads as
        # dead and the caller burns its one retry.
        sock = mock.Mock()
        reader = mock.Mock()
        reader.readline.return_value = b'{"ok": true}\n'
        lease = bb.Lease(sock, reader, 41999, 41_000, True, False, bb.PROTOCOL_VERSION, True,
                         None)
        self.assertTrue(lease.live())
        self.assertEqual(sock.settimeout.call_args.args[0], bb._HELLO_TIMEOUT)

    def test_a_status_call_waits_as_long_as_a_hello(self):
        # ``status`` is what `_others_on` asks to decide a replacement; on the 2 s default a
        # heal would time out and the broker would be replaced under its own clients.
        self.assertEqual(bb.status.__kwdefaults__["timeout"], bb._HELLO_TIMEOUT)

    def test_a_failed_heal_answers_with_its_reason(self):
        # The broker leaves after a failed heal; without the reason on the reply the client
        # attaches to the port of the browser that is gone and reports only that.
        backend = _backend_from(bb.Backend(41_001, True, None),
                                bb.BrowserUnavailable("no usable browser here"),
                                bb.BrowserUnavailable("no usable browser here"))
        b, port = _serve(backend=backend, idle_minutes=0)
        err = io.StringIO()
        try:
            with mock.patch.object(bb, "cdp_ready", return_value=False), \
                    mock.patch.object(bb, "_HOLD_SETTLE", 0.05), \
                    mock.patch.object(bb.sys, "stderr", err), \
                    self.assertRaises(bb.BrokerUnavailable) as caught:
                bb.status(port)
        finally:
            b.stop()
        self.assertIn("no usable browser here", str(caught.exception))


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

    def test_a_reader_never_sees_half_a_written_session_file(self):
        b, port = _serve(clock=self.clock)
        try:
            seen = []
            real_dump = json.dump

            def dump(obj, fh, **kw):
                try:
                    seen.append(paths.browser_session_file().read_text())
                except FileNotFoundError:
                    seen.append(None)
                return real_dump(obj, fh, **kw)

            with mock.patch.object(bb.json, "dump", side_effect=dump):
                b._write_session(b._be)
            self.assertTrue(seen)
            for text in seen:
                self.assertIsInstance(json.loads(text), dict)
            self.assertEqual(bb.read_session()["control_port"], port)
            self.assertFalse(paths.browser_session_file().with_name(
                paths.browser_session_file().name + ".tmp").exists())
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
        with bb.profile_lock(paths.browser_profile_dir()), \
                self.assertRaises(TimeoutError), \
                bb.profile_lock(paths.browser_profile_dir(), timeout=0.2):
            pass


def _fake_chrome(port: int = 4321, *, announce: bool = True):
    """A Popen stand-in that behaves like Chrome: announces its port, then stays up.

    ``announce=False`` is the older shape: no line on stderr, the port only in the file.
    """
    def popen(argv, **_kw):
        child = mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        child.stderr = iter(
            [b"warn\n",
             f"DevTools listening on ws://127.0.0.1:{port}/devtools/browser/u\n".encode()]
            if announce else [])
        if not announce:
            (paths.browser_profile_dir() / "DevToolsActivePort").write_text(
                f"{port}\n/devtools/browser/x\n", encoding="utf-8")
        return child
    return popen


class PrepareTest(BrokerTest):
    def setUp(self):
        super().setUp()
        paths.browser_profile_dir().mkdir(parents=True, exist_ok=True)

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
        with mock.patch.object(bb, "_HOLD_SETTLE", 0.1), \
                mock.patch.object(bb, "live_port", return_value=None), \
                mock.patch.object(bb, "profile_holder", return_value=4242), \
                self.assertRaises(bb.ProfileBusy) as ctx:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(str(ctx.exception), bb._BUSY_SENTENCE)

    def test_a_holder_that_clears_itself_is_waited_for_instead_of_reported(self):
        # The browser being replaced takes a moment to go; a replacement broker arrives in the
        # middle of it and must not call a dying browser "yours, close it".
        gone = iter([4242, 4242, None])

        def holder(_profile):
            return next(gone, None)

        with mock.patch.object(bb, "live_port", return_value=None), \
                mock.patch.object(bb, "profile_holder", side_effect=holder), \
                mock.patch.object(bb, "_reap"), \
                mock.patch.object(bb, "_launch_detached",
                                  return_value=bb.BrowserRuntime(7123, None)) as launch:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        launch.assert_called_once()

    def test_the_profile_directory_and_headless_flag_reach_the_command_line(self):
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, "Profile 1")
        argv = popen.call_args.args[0]
        self.assertIn("--profile-directory=Profile 1", argv)
        self.assertIn("--headless=new", argv)

    def test_the_profile_is_always_named_even_when_it_is_the_platform_default(self):
        # A headless Chrome started without --user-data-dir takes a throwaway scoped_dir
        # instead of its default one, and with it an empty cookie store: the user's sign-ins
        # are not there. Only naming the directory points the browser at their real profile.
        with mock.patch.object(bb.paths, "user_browser_profile_dir",
                               return_value=paths.browser_profile_dir()), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), \
                mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, "Default")
        argv = popen.call_args.args[0]
        self.assertIn(f"--user-data-dir={paths.browser_profile_dir()}", argv)

    def test_any_other_profile_is_named_too(self):
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/bin/true", Path("/tmp/some-profile"), True, None, "Default")
        self.assertIn("--user-data-dir=/tmp/some-profile", popen.call_args.args[0])

    def test_the_users_own_profile_child_gets_a_private_config_home(self):
        # Naming the default directory is not enough: Chrome refuses a debugging port while
        # --user-data-dir resolves to its platform default, and it works that default out from
        # XDG_CONFIG_HOME. A private XDG for the child makes the named path non-default again;
        # the cookie store is still the named, real one.
        with mock.patch.object(bb.paths, "user_browser_profile_dir",
                               return_value=paths.browser_profile_dir()), \
                mock.patch.dict(os.environ,
                                {"XDG_CONFIG_HOME": "/nowhere",
                                 "XDG_DATA_HOME": self._home.name}), \
                mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["XDG_CONFIG_HOME"], str(paths.browser_private_xdg()))
            self.assertNotEqual(env["XDG_CONFIG_HOME"], "/nowhere")
            # It is the child's copy that changes; this process keeps its own environment,
            # and a data home of the caller's would move the browser's state along with it.
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], "/nowhere")
        self.assertNotIn("XDG_DATA_HOME", env)

    def test_another_profile_child_keeps_the_inherited_environment(self):
        # The dedicated directory is nobody's default, so the port opens there without any
        # help and the child's environment is left exactly as it was.
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        self.assertIsNone(popen.call_args.kwargs["env"])

    def test_a_browser_that_never_opens_a_port_is_killed(self):
        # Left alive it sits on the profile with a SingletonLock and no DevToolsActivePort:
        # nothing can attach, the next launch reads a busy profile, and `browser stop` says
        # "no session" because no session file was ever written.
        with mock.patch.object(bb, "_READY_TIMEOUT", 0.3), \
                mock.patch.object(bb, "cdp_ready", return_value=False), \
                mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()), \
                mock.patch.object(bb.os, "kill") as kill, \
                self.assertRaises(bb.BrowserUnavailable):
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        kill.assert_called_once_with(4242, bb._KILL)

    def test_the_channel_and_proxy_reach_prepare(self):
        # `[browser].channel` and `[network].proxy` are parsed by main() and handed on; dropping
        # them starts the bundled Chromium on a direct connection while the request path leaves
        # by the proxy, and a clearance is bound to the IP it was earned on.
        with mock.patch.object(bb, "prepare",
                               return_value=bb.Backend(1, True, None)) as prep:
            broker = bb.Broker(profile=paths.browser_profile_dir(), channel="google-chrome",
                               proxy="http://127.0.0.1:7890")
            broker._bring_up()
        prep.assert_called_once_with(broker.profile, broker.headless, broker.profile_directory,
                                     "google-chrome", "http://127.0.0.1:7890")

    def test_main_hands_the_channel_and_proxy_to_the_broker(self):
        with mock.patch.object(bb, "Broker") as made:
            made.return_value.serve.return_value = 0
            self.assertEqual(bb.main(["--profile", "/p", "--channel", "google-chrome",
                                      "--proxy", "http://p:1"]), 0)
        self.assertEqual(made.call_args.kwargs["channel"], "google-chrome")
        self.assertEqual(made.call_args.kwargs["proxy"], "http://p:1")

    def test_the_users_own_profile_is_started_headless_even_when_a_window_was_asked_for(self):
        # A visible Chrome gives no debugging port on the platform default data directory, and
        # the user's own profile is that directory. Without a port there is no session at all,
        # so the shared browser on it is always hidden; `browser login` is how a window opens.
        with mock.patch.object(bb.paths, "user_browser_profile_dir",
                               return_value=paths.browser_profile_dir()):
            broker = bb.Broker(profile=paths.browser_profile_dir(), headless=False)
        self.assertTrue(broker.headless)

    def test_a_dedicated_profile_stays_headed_when_asked(self):
        with mock.patch.object(bb.paths, "user_browser_profile_dir",
                               return_value=Path("/nowhere/at/all")):
            broker = bb.Broker(profile=paths.browser_profile_dir(), headless=False)
        self.assertFalse(broker.headless)

    def test_the_debugging_port_is_left_to_chrome_and_read_back_from_the_file(self):
        # Chrome writes DevToolsActivePort only when the port is left at 0. A port we pick
        # leaves no file behind, so nothing could find the browser again after a broker kill.
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", side_effect=lambda port, **_: port == 4321), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            out = bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        self.assertIn("--remote-debugging-port=0", popen.call_args.args[0])
        self.assertEqual(out.port, 4321)

    def test_the_announced_port_is_used_even_with_a_stale_file_beside_it(self):
        # Only the first browser on a data directory writes DevToolsActivePort; the user's own
        # directory is shared with whatever else is open, so the announcement is the source.
        paths.browser_profile_dir().joinpath("DevToolsActivePort").write_text(
            "9999\n/devtools/browser/old\n", encoding="utf-8")
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()), \
                mock.patch.object(bb, "cdp_ready", side_effect=lambda port, **_: port == 4321), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            out = bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        self.assertEqual(out.port, 4321)

    def test_a_browser_that_does_not_announce_is_still_found_by_its_file(self):
        with mock.patch.object(bb.subprocess, "Popen",
                               side_effect=_fake_chrome(announce=False)), \
                mock.patch.object(bb, "cdp_ready", side_effect=lambda port, **_: port == 4321), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            out = bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        self.assertEqual(out.port, 4321)

    def test_the_launch_writes_its_port_where_the_next_command_looks(self):
        marker = paths.browser_profile_dir() / "DevToolsActivePort"
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()), \
                mock.patch.object(bb, "cdp_ready", side_effect=lambda port, **_: port == 4321), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        self.assertEqual(marker.read_text().splitlines()[0], "4321")

    def test_a_port_file_from_before_the_launch_is_not_taken_for_the_new_port(self):
        paths.browser_profile_dir().joinpath("DevToolsActivePort").write_text(
            "9999\n/devtools/browser/old\n", encoding="utf-8")
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()), \
                mock.patch.object(bb, "cdp_ready", side_effect=lambda port, **_: port == 4321), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            out = bb._launch_detached("/bin/true", paths.browser_profile_dir(), True, None, None)
        self.assertEqual(out.port, 4321)

    def test_without_any_chrome_the_bundled_browser_is_launched(self):
        with mock.patch.object(bb.shutil, "which", return_value=None), \
                mock.patch.object(bb, "_bundled_binary", return_value="/b/chrome"), \
                mock.patch.object(bb, "live_port", return_value=None), \
                mock.patch.object(bb, "profile_holder", return_value=None), \
                mock.patch.object(bb, "_launch_detached",
                                  return_value=bb.BrowserRuntime(7123, None)) as launch:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        self.assertEqual(launch.call_args.args[0], "/b/chrome")

    def test_the_bundled_browser_is_launched_without_a_sandbox(self):
        # Playwright's Chromium ships chrome_sandbox non-setuid, so on a machine that also
        # restricts unprivileged user namespaces it aborts with "No usable sandbox".
        with mock.patch.object(bb.subprocess, "Popen", side_effect=_fake_chrome()) as popen, \
                mock.patch.object(bb, "cdp_ready", return_value=True), \
                mock.patch.object(bb, "_attach_engine", return_value=(object(), None)):
            bb._launch_detached("/b/chrome", paths.browser_profile_dir(), True, None,
                                no_sandbox=True)
        self.assertIn("--no-sandbox", popen.call_args.args[0])

    def test_a_chrome_found_on_the_path_keeps_its_sandbox(self):
        with mock.patch.object(bb.shutil, "which", return_value="/usr/bin/google-chrome"), \
                mock.patch.object(bb, "live_port", return_value=None), \
                mock.patch.object(bb, "profile_holder", return_value=None), \
                mock.patch.object(bb, "_launch_detached",
                                  return_value=bb.BrowserRuntime(7123, None)) as launch:
            bb.prepare(paths.browser_profile_dir(), True, None, None, None)
        self.assertFalse(launch.call_args.kwargs.get("no_sandbox", False))

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


class RunningLookupTest(BrokerTest):
    def test_running_browsers_are_found_without_their_proc_entry_matching(self):
        # Chrome rewrites its own argv into one space-joined string, so NUL-splitting
        # /proc/<pid>/cmdline never yields an argument and a per-argument match finds nothing.
        # A profile that only shares a prefix with ours is someone else's browser.
        proc = Path(self._home.name) / "proc"
        mine = str(paths.browser_profile_dir())

        def entry(pid, text):
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(text.encode())

        entry("100", f"/opt/google/chrome/chrome --headless=new --user-data-dir={mine} "
                     "--remote-debugging-port=9333 --no-first-run about:blank")
        entry("101", "\0".join(["/opt/google/chrome/chrome", f"--user-data-dir={mine}",
                                "--remote-debugging-port=9444"]))
        entry("102", f"/opt/google/chrome/chrome --user-data-dir={mine}2 "
                     "--remote-debugging-port=9555")
        entry("103", f"/opt/google/chrome/chrome --type=zygote --user-data-dir={mine} "
                     "--remote-debugging-port=9666")
        entry("104", f"/opt/google/chrome/chrome --user-data-dir={mine} --no-first-run")
        (proc / "self").mkdir()
        # 103 is a child (its port is the parent's): reaping it would kill a renderer, so it is
        # left out. 104 is a browser started without a debugging port — invisible, but a browser.
        self.assertEqual(bb._running_on(paths.browser_profile_dir(), proc),
                         [(100, 9333), (101, 9444), (104, None)])


class WindowsTest(BrokerTest):
    """The Windows shapes: a detached spawn, a byte lock, and no /proc to read."""

    def test_windows_spawn_is_detached(self):
        with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
                mock.patch.object(bb.subprocess, "Popen") as popen:
            bb._spawn_broker(["x"])
        self.assertIn("creationflags", popen.call_args.kwargs)
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertNotIn("start_new_session", popen.call_args.kwargs)

    def test_windows_lock_uses_locking_and_unlocks_on_exit(self):
        with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
                mock.patch.object(bb, "msvcrt") as lock, bb.profile_lock(paths.browser_profile_dir()):
            pass
        self.assertEqual([c.args[1] for c in lock.locking.call_args_list],
                         [lock.LK_NBLCK, lock.LK_UNLCK])

    def test_a_busy_windows_lock_times_out(self):
        with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
                mock.patch.object(bb, "msvcrt") as lock:
            lock.locking.side_effect = OSError("locked")
            with self.assertRaises(TimeoutError), bb.profile_lock(paths.browser_profile_dir(), timeout=0.1):
                pass

    def test_a_lock_written_as_a_plain_file_still_names_its_pid(self):
        # A symlink needs privilege on Windows, so the lock is a file with "<host>-<pid>" in it.
        # Without the text fallback the busy check finds nobody and launches into a held
        # profile, which only surfaces 20 s later as "never opened its debugging port".
        # An explicit directory: ``_IS_WINDOWS`` also moves where the default one lives.
        profile = Path(self._home.name) / "prof"
        profile.mkdir(parents=True)
        (profile / "SingletonLock").write_text(f"{socket.gethostname()}-4242")
        with mock.patch.object(bb, "os") as os_mod, \
                mock.patch.object(bb, "_pid_alive", return_value=True):
            os_mod.readlink.side_effect = OSError("not a link")
            os_mod.path = os.path
            self.assertEqual(bb.profile_holder(profile), 4242)

    def test_a_lock_left_by_a_dead_pid_names_no_holder(self):
        # Chrome does not clean the lock up after a crash, and on Windows a pid number is the
        # whole story unless it is checked. A stale lock read as a live holder blocks every
        # launch on the profile until the pid number is reused.
        profile = Path(self._home.name) / "prof"
        profile.mkdir(parents=True)
        (profile / "SingletonLock").write_text(f"{socket.gethostname()}-4242")
        with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
                mock.patch.object(bb, "os") as os_mod, \
                mock.patch.object(bb, "_pid_alive", return_value=False):
            os_mod.readlink.side_effect = OSError("not a link")
            os_mod.path = os.path
            self.assertIsNone(bb.profile_holder(profile))

    def test_the_holder_probe_does_not_terminate_what_it_probes(self):
        # os.kill(pid, 0) is TerminateProcess on Windows, so probing with it would kill the
        # browser that is being checked.
        profile = Path(self._home.name) / "prof"
        profile.mkdir(parents=True)
        (profile / "SingletonLock").write_text(f"{socket.gethostname()}-4242")
        with mock.patch.object(bb.paths, "_IS_WINDOWS", True), \
                mock.patch.object(bb, "os") as os_mod, \
                mock.patch.object(bb, "_pid_alive", return_value=True):
            os_mod.readlink.side_effect = OSError("not a link")
            os_mod.path = os.path
            self.assertEqual(bb.profile_holder(profile), 4242)
        os_mod.kill.assert_not_called()

    def test_a_leftover_on_a_profile_without_proc_is_reaped_through_its_lock(self):
        # No /proc: the lock is the only place a leftover can be found, and without this the
        # dedicated directory fills up with dead browsers run after run.
        profile = paths.browser_profile_dir()
        with mock.patch.object(bb, "_running_on", return_value=[]), \
                mock.patch.object(bb, "_PROC", Path("/no/such/proc")), \
                mock.patch.object(bb, "profile_holder", return_value=4242), \
                mock.patch.object(bb.os, "kill") as killed:
            bb._reap(profile, keep=None)
        killed.assert_called_once_with(4242, bb.signal.SIGTERM)

    def test_headless_lookup_without_proc_says_headed(self):
        # /proc is a Linux thing. Without it a leftover reads as headed, which is the safe
        # way round: a headed browser is left alone.
        with mock.patch.object(bb, "_PROC", Path("/no/such/proc")):
            self.assertFalse(bb._is_headless(os.getpid()))


class KillTest(unittest.TestCase):
    def test_the_escalation_uses_a_signal_the_platform_has(self):
        # Windows has no SIGKILL; asking for it raises AttributeError outside the suppress and
        # takes the broker down with a traceback nobody reads.
        with mock.patch.object(bb, "_KILL", 9), mock.patch.object(bb.os, "kill") as killed:
            bb._wait_gone(99, timeout=0)
        killed.assert_called_once_with(99, 9)


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


class JoinProfileTest(BrokerTest):
    def test_a_broker_serving_another_profile_is_not_joined(self):
        port = 41999
        info = {"control_port": port, "profile": "/old/profile"}
        with mock.patch.object(bb, "connect", return_value="lease") as joined:
            self.assertIsNone(bb._join(info, bb.PROTOCOL_VERSION, True, Path("/new")))
        joined.assert_not_called()
        with mock.patch.object(bb, "connect", return_value="lease"):
            self.assertEqual(bb._join(info, bb.PROTOCOL_VERSION, True, Path("/old/profile")),
                             "lease")

    def test_a_profile_spelled_differently_is_the_same_profile(self):
        # `/p/`, `/p/.` and a symlink to `/p` are one directory; comparing the strings spends
        # the running broker on a spelling.
        info = {"control_port": 41999, "profile": "/p/"}
        with mock.patch.object(bb, "connect", return_value="lease"):
            self.assertEqual(bb._join(info, bb.PROTOCOL_VERSION, True, Path("/p")), "lease")

    def test_a_file_without_a_profile_is_joined(self):
        # Older brokers and the test doubles do not carry the field; guessing "different"
        # would replace a perfectly good broker on every command.
        with mock.patch.object(bb, "connect", return_value="lease"):
            self.assertEqual(bb._join({"control_port": 41999}, bb.PROTOCOL_VERSION, True,
                                      Path("/new")), "lease")

    def test_a_broker_only_removes_a_discovery_file_that_is_still_its_own(self):
        # After a profile change two brokers overlap; the one leaving must not delete the
        # newcomer's file.
        b, _port = _serve(backend=lambda: bb.Backend(41_000, True, None), idle_minutes=0)
        paths.browser_session_file().write_text(json.dumps({"control_port": 1}))
        b.stop()
        self.assertEqual(bb.read_session()["control_port"], 1)


class EnsureLeaseTest(BrokerTest):
    def test_a_changed_profile_retires_the_broker_of_the_old_one(self):
        # `[browser].profile` was edited while a broker ran on the old profile. Joining it
        # would hand back the old port and the setting would silently do nothing.
        stale = {"control_port": 41998, "profile": "/old/profile"}
        with mock.patch.object(bb, "read_session", return_value=stale), \
                mock.patch.object(bb, "shutdown") as stopped, \
                mock.patch.object(bb, "_await_retired") as waited, \
                mock.patch.object(bb, "connect", return_value="lease"):
            lease = bb.ensure_lease(paths.browser_profile_dir(), headless=True,
                                    spawn=_spawn_that_brings_up(41998))
        self.assertEqual(lease, "lease")
        stopped.assert_called_once_with(41998, force=True)
        waited.assert_called_once_with(41998)

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
        with mock.patch.object(bb, "_spawn_broker", return_value=mock.Mock(poll=lambda: None)), \
                self.assertRaises(bb.BrokerUnavailable):
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


class CdpProbeTest(BrokerTest):
    def _answering(self, payload: bytes) -> int:
        """A listener that sends ``payload`` to one caller and closes; its port."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def serve():
            conn, _peer = server.accept()
            with conn:
                conn.sendall(payload)

        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()[1]

    def test_a_line_that_is_not_http_is_not_ready(self):
        # A broker control port answers one JSON line. Probed as a CDP port that is a
        # BadStatusLine -- an HTTPException, not an OSError -- and it used to escape the probe
        # and kill whatever thread was serving a command.
        port = self._answering(b'{"error": "one JSON object with a cmd per line"}\n')
        self.assertFalse(bb.cdp_ready(port, timeout=1.0))


class MainEntryTest(unittest.TestCase):
    """The module's own entry point, the way production spawns it: ``python -m``."""

    def test_a_failed_launch_leaves_a_message_not_a_traceback(self):
        # `-m` runs this file as __main__, a second module holding a second copy of every
        # class, while relative imports get the package's copies back. An except clause in the
        # __main__ copy then does not match what `prepare` raises, and the failure escapes as a
        # traceback on a stderr pipe nobody reads.
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright is not installed")
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "PATH": str(Path(home) / "nobin"),
                   "XDG_DATA_HOME": str(Path(home) / "data"),
                   "PLAYWRIGHT_BROWSERS_PATH": str(Path(home) / "nobrowsers")}
            proc = subprocess.run(
                [sys.executable, "-m", "litcli.browser_broker",
                 "--profile", str(Path(home) / "prof"), "--headless"],
                cwd=repo, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("could not start", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
