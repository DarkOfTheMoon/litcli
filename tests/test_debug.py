"""``--debug`` / ``LITCLI_DEBUG``: the trace is opt-in, masked and goes to stderr only."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import cli, debug
from litcli.commands import parser as parser_mod
from litcli.transport import client


def _capture(fn):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = fn()
    return result, err.getvalue()


class DebugStateTest(unittest.TestCase):
    def setUp(self):
        self._saved = debug._ENABLED
        debug._ENABLED = False
        self.addCleanup(setattr, debug, "_ENABLED", self._saved)

    def test_off_by_default(self):
        self.assertFalse(debug.enabled())
        _, err = _capture(lambda: debug.log("hidden"))
        self.assertEqual(err, "")

    def test_the_flag_turns_it_on(self):
        debug.set_enabled()
        self.assertTrue(debug.enabled())

    def test_the_environment_turns_it_on(self):
        # Read at import; simulate the import-time read for a fresh process.
        with mock.patch.dict(os.environ, {"LITCLI_DEBUG": "1"}):
            import importlib
            importlib.reload(debug)
        self.addCleanup(importlib.reload, debug)
        self.assertTrue(debug.enabled())

    def test_the_environment_does_not_turn_it_off(self):
        debug.set_enabled()
        with mock.patch.dict(os.environ, {"LITCLI_DEBUG": "0"}):
            debug.set_enabled()
        self.assertTrue(debug.enabled())


class MaskTest(unittest.TestCase):
    def test_credentials_and_the_polite_pool_address_are_hidden(self):
        url = "https://api.example/x?email=me@x.org&api_key=SECRET&year=2020"
        masked = debug.mask(url)
        self.assertIn("email=***", masked)
        self.assertIn("api_key=***", masked)
        self.assertIn("year=2020", masked)
        self.assertNotIn("SECRET", masked)
        self.assertNotIn("me@x.org", masked)


class HookTest(unittest.TestCase):
    def setUp(self):
        self._saved = debug._ENABLED
        debug._ENABLED = True
        self.addCleanup(setattr, debug, "_ENABLED", self._saved)

    def test_a_request_and_its_status_are_both_traced(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
        with client(transport=transport) as c:
            _, err = _capture(lambda: c.get("https://api.example/x?api_key=K"))
        self.assertIn("--> GET https://api.example/x?api_key=***", err)
        self.assertIn("<-- 200", err)

    def test_a_failed_status_is_still_traced(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(404))
        with client(transport=transport) as c:
            _, err = _capture(lambda: c.get("https://api.example/missing"))
        self.assertIn("<-- 404", err)

    def test_retry_lines_render_with_the_attempt_and_the_first_failure_line(self):
        from litcli.backends.http import _log_retry
        state = mock.Mock(attempt_number=2, next_action=mock.Mock(sleep=1.5))
        state.outcome.exception.return_value = RuntimeError("HTTP 503\nbody here")
        _, err = _capture(lambda: _log_retry(state))
        self.assertEqual(err.strip(), "[debug] retry 2 in 1.5s: HTTP 503")

    def test_no_hooks_are_installed_when_off(self):
        debug._ENABLED = False
        self.assertEqual(debug.hooks(), {})

    def test_the_page_body_is_not_dumped(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="x" * 5000))
        with client(transport=transport) as c:
            _, err = _capture(lambda: c.get("https://api.example/x"))
        self.assertLess(len(err), 400)


class FlagTest(unittest.TestCase):
    def setUp(self):
        self._saved = debug._ENABLED
        debug._ENABLED = False
        self.addCleanup(setattr, debug, "_ENABLED", self._saved)

    def test_the_parser_accepts_it_before_the_command(self):
        args = parser_mod.build_parser().parse_args(["--debug", "search", "x"])
        self.assertTrue(args.debug)

    def test_main_accepts_it_after_the_command_too(self):
        err = io.StringIO()
        with mock.patch.dict(cli._TOP_LEVEL_COMMANDS, {"search": mock.Mock(return_value=0)}), \
                mock.patch.object(cli, "load_config", return_value={}), \
                mock.patch.object(cli, "_apply_proxy"), \
                contextlib.redirect_stderr(err):
            cli.main(["search", "x", "--debug"])
        self.assertTrue(debug.enabled())
        self.assertIn("[debug] litcli", err.getvalue())

    def _main(self, argv, **patches):
        ctx = [mock.patch.object(cli, k, **v) for k, v in patches.items()]
        for c in ctx:
            c.start()
            self.addCleanup(c.stop)
        return cli.main(argv)

    def test_it_is_not_printable_to_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(cli._TOP_LEVEL_COMMANDS, {"search": mock.Mock(return_value=0)}), \
                mock.patch.object(cli, "load_config", return_value={}), \
                mock.patch.object(cli, "_apply_proxy"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(["--debug", "search", "x"])
        self.assertTrue(debug.enabled())
        self.assertIn("[debug]", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_an_unexpected_failure_shows_a_traceback_only_with_the_flag(self):
        boom = ZeroDivisionError("boom")
        for argv, expected in ((["--debug", "search", "x"], True), (["search", "x"], False)):
            debug._ENABLED = False
            err = io.StringIO()
            with mock.patch.object(cli, "load_config", side_effect=boom), \
                    mock.patch.object(cli, "_apply_proxy"), \
                    contextlib.redirect_stderr(err), \
                    self.assertRaises(ZeroDivisionError):
                cli.main(argv)
            self.assertEqual("Traceback" in err.getvalue(), expected, argv)


if __name__ == "__main__":
    unittest.main()
