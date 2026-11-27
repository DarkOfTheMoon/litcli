"""The broker's control port: one line per command, EOF is a released lease."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
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
