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
        if paths.browser_session_file().exists():
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
