"""The failure taxonomy: kinds, HTTP mapping, and the CLI's policy on each.

The point of the taxonomy is that no caller has to read a message to decide what to do: ``kind``
carries the decision and the CLI maps it to an exit code once.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import cli, errors
from litcli.backends.base import Backend, NotSupportedError
from litcli.commands import search
from litcli.errors import (
    AuthRequired,
    BadInput,
    BotWall,
    External,
    LitcliError,
    NotFound,
    Paywalled,
    Unavailable,
    Unsupported,
    for_status,
    http_kind,
)
from litcli.transport import HttpError


class _Stub(Backend):
    name = "stub"

    def search(self, query, limit=20, filters=None):
        return []

    def get(self, key):
        raise KeyError(key)


class KindTest(unittest.TestCase):
    def test_every_class_carries_its_own_kind(self):
        for cls, kind in ((BadInput, "bad-input"), (NotFound, "not-found"), (Paywalled, "paywalled"),
                          (AuthRequired, "auth"), (BotWall, "bot-wall"), (Unavailable, "unavailable"),
                          (Unsupported, "unsupported"), (External, "external")):
            self.assertTrue(issubclass(cls, LitcliError))
            self.assertEqual(cls("x").kind, kind)

    def test_the_default_kind_is_a_plain_error(self):
        self.assertEqual(LitcliError("x").kind, "error")

    def test_hint_is_kept_beside_the_message(self):
        e = External("no browser", hint="litcli backends install-browser")
        self.assertEqual(str(e), "no browser")
        self.assertEqual(e.hint, "litcli backends install-browser")

    def test_not_supported_keeps_the_notimplementederror_contract(self):
        # Existing ``except NotImplementedError`` sites must survive the typed hierarchy.
        e = NotSupportedError("write", _Stub())
        self.assertIsInstance(e, Unsupported)
        self.assertIsInstance(e, NotImplementedError)


class HttpKindTest(unittest.TestCase):
    def test_statuses_map_to_the_meaning_the_sources_give_them(self):
        cases = {401: AuthRequired, 402: Paywalled, 451: Paywalled, 403: BotWall,
                 404: NotFound, 410: NotFound, 400: BadInput, 422: BadInput,
                 500: Unavailable, 406: Unavailable}
        for status, cls in cases.items():
            self.assertIs(http_kind(status), cls, status)

    def test_http_error_takes_its_kind_and_stays_an_unavailable_class(self):
        e = HttpError("crossref", 404, "Not Found")
        self.assertEqual(e.kind, "not-found")
        self.assertIsInstance(e, Unavailable)  # plain ``except Unavailable`` still means "network"
        self.assertEqual(e.status, 404)

    def test_for_status_builds_the_same_classification(self):
        self.assertIsInstance(for_status(403, "wall"), BotWall)
        self.assertIsInstance(for_status(503, "down"), Unavailable)


class CliPolicyTest(unittest.TestCase):
    def _run(self, exc):
        parser = mock.Mock()
        parser.parse_args.return_value = mock.Mock(cmd="search", data_cmd=None)
        with mock.patch.object(cli, "build_parser", return_value=parser), \
                mock.patch.object(cli, "load_config", return_value={}), \
                mock.patch.object(cli, "_apply_proxy"), \
                mock.patch.dict(cli._TOP_LEVEL_COMMANDS, {"search": mock.Mock(side_effect=exc)}), \
                mock.patch("builtins.print") as printed:
            rc = cli.main([])
        return rc, [c.args[0] for c in printed.call_args_list]

    def test_a_typed_failure_is_reported_with_its_exit_code(self):
        rc, out = self._run(NotFound("[crossref] HTTP 404: Not Found"))
        self.assertEqual(rc, 2)
        self.assertEqual(out, ["[error] [crossref] HTTP 404: Not Found"])

    def test_unsupported_reads_as_skipped_not_failed(self):
        rc, out = self._run(Unsupported("[arxiv] no support"))
        self.assertEqual(rc, 2)
        self.assertEqual(out, ["[skipped] [arxiv] no support"])

    def test_the_hint_is_printed_on_its_own_line(self):
        _rc, out = self._run(External("browser missing", hint="litcli backends install-browser"))
        self.assertEqual(out, ["[error] browser missing", "[hint] litcli backends install-browser"])


class AllPolicyTest(unittest.TestCase):
    """``--all`` skips a failing backend instead of sinking the aggregation — silently when the
    failure is the backend's normal state."""

    def _search(self, exc):
        args = mock.Mock(backend=None, query="q", limit=5, format="lines", no_cite=False,
                         type=None, sort=None, title=None, author=None, year=None, container=None)
        good = mock.Mock()
        good.search.return_value = []
        bad = mock.Mock()
        bad.search.side_effect = exc
        with mock.patch.object(search, "get_all_backends", return_value={"bad": bad, "good": good}), \
                mock.patch("builtins.print") as printed, \
                mock.patch.object(search, "print_items", return_value=0):
            rc = search.cmd_search({}, args)
        return rc, [c.args[0] for c in printed.call_args_list if c.args]

    def test_an_unsupported_backend_is_silent(self):
        _, out = self._search(NotSupportedError("search", _Stub()))
        self.assertEqual(out, [])

    def test_other_failures_are_one_line_with_the_hint(self):
        _, out = self._search(AuthRequired("key missing", hint="set api_key"))
        self.assertEqual(out, ["[skipped] backend bad: key missing (set api_key)"])

    def test_a_stray_runtime_failure_still_only_skips(self):
        _, out = self._search(RuntimeError("boom"))
        self.assertEqual(out, ["[skipped] backend bad: boom"])

    def test_the_search_survives_to_the_result_line(self):
        rc, _ = self._search(Unavailable("down"))
        self.assertEqual(rc, 0)


class ExitCodeTest(unittest.TestCase):
    def test_the_exit_code_is_defined_once(self):
        self.assertEqual(errors.ERROR_EXIT, 2)
        self.assertEqual(AuthRequired("x").exit_code, errors.ERROR_EXIT)


if __name__ == "__main__":
    unittest.main()
