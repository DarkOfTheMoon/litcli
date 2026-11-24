"""Config backend-discovery unit tests: pass a dict directly, no disk write, no network."""
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from litcli import config
from litcli.errors import BadInput


class TestConfig(unittest.TestCase):
    def setUp(self):
        config.failed_backends.clear()
        self.addCleanup(config.failed_backends.clear)

    def test_discover_inbuilt(self):
        backends = config.discover_backends(
            {"backends": {"zotero": {"type": "zotero_local", "base": "http://x"}}})
        self.assertIn("zotero", backends)
        self.assertEqual(sorted(backends["zotero"].capabilities),
                         ["attachment", "read", "write"])

    def test_unknown_type_is_collected_like_any_other_failure(self):
        # It used to re-raise past ``strict`` and take every other backend down with it.
        out = config.discover_backends({"backends": {"good": {"type": "arxiv"},
                                                     "x": {"type": "nope"}}})
        self.assertIn("good", out)
        self.assertIn("unknown backend type", config.failed_backends["x"])

    def test_unknown_type_still_raises_under_strict(self):
        with self.assertRaises(KeyError):
            config.discover_backends({"backends": {"x": {"type": "nope"}}}, strict=True)

    def test_an_explicit_backend_that_failed_still_raises(self):
        cfg = {"backends": {"x": {"type": "nope"}}}
        with self.assertRaises(RuntimeError) as caught:
            config.get_backend("x", config=cfg)
        self.assertIn("unknown backend type", str(caught.exception))

    def test_enabled_false_skips_the_backend(self):
        out = config.discover_backends({
            "backends": {"on": {"type": "arxiv"},
                         "off": {"type": "arxiv", "enabled": False}}})
        self.assertIn("on", out)
        self.assertNotIn("off", out)
        self.assertNotIn("off", config.failed_backends)

    def test_enabled_true_is_just_another_config_key(self):
        # ``enabled`` must not reach the constructor as a kwarg (it used to kill the backend).
        out = config.discover_backends({"backends": {"a": {"type": "arxiv", "enabled": True}}})
        self.assertIn("a", out)

    def test_a_name_key_does_not_collide_with_the_section_identity(self):
        out = config.discover_backends({"backends": {"a": {"type": "arxiv", "name": "other"}}})
        self.assertEqual(out["a"].name, "a")

    def test_a_non_table_section_is_collected(self):
        out = config.discover_backends({"backends": {"good": {"type": "arxiv"}, "bad": "x"}})
        self.assertIn("good", out)
        self.assertIn("not a table", config.failed_backends["bad"])

    def test_corpora_loop_tolerates_a_broken_optional_source(self):
        def fake(name, cfg):
            if name == "doaj":
                raise RuntimeError("broken doaj section")
            return mock.Mock(name=name)

        with mock.patch.object(config, "get_backend", side_effect=fake):
            out = config.get_all_backends({"backends": {}}, corpora=True)
        self.assertNotIn("doaj", out)
        self.assertIn("europepmc", out)

    def test_a_disabled_backend_is_not_rebuilt_by_get_backend(self):
        # ``fetch`` rebuilds its corpora and publisher steps through ``get_backend``; the
        # registry fallback used to hand a switched-off section straight back.
        with self.assertRaises(RuntimeError) as got:
            config.get_backend("europepmc", {"backends": {"europepmc": {"enabled": False}}})
        self.assertIn("disabled", str(got.exception))

    def test_a_disabled_corpus_is_not_added_by_the_corpora_loop(self):
        out = config.get_all_backends({"backends": {"europepmc": {"enabled": False}}},
                                      corpora=True)
        self.assertNotIn("europepmc", out)

    def test_a_reader_that_cannot_be_imported_reports_its_own_cause(self):
        # Keeping the rest alive must not cost the reason: the registry swallowed the exception,
        # so the report fell back to "unknown backend type" — a name the config plainly has —
        # while the real cause (a bad constant) vanished. Both guard shapes are covered: a
        # plain optional backend and a table-driven publisher.
        import importlib.abc
        import importlib.machinery
        import sys

        for broken, other in (("arxiv", "crossref"), ("acm", "aps")):
            with self.subTest(broken):
                # The defaults bind the loop variables at class-creation time.
                class Boom(importlib.abc.MetaPathFinder, importlib.abc.Loader):
                    def find_spec(self, name, path=None, target=None, mod=f"litcli.backends.{broken}"):
                        if name == mod:
                            return importlib.machinery.ModuleSpec(name, self)

                    def create_module(self, spec):
                        return None

                    def exec_module(self, module, why=f"bad constant in {broken}"):
                        raise ValueError(why)

                config._registry.cache_clear()
                config._IMPORT_ERRORS.clear()
                config.failed_backends.clear()
                self.addCleanup(config._registry.cache_clear)
                # An imported module is served from ``sys.modules`` without asking ``meta_path``.
                cached = sys.modules.pop(f"litcli.backends.{broken}", None)
                if cached is not None:
                    self.addCleanup(sys.modules.__setitem__, f"litcli.backends.{broken}", cached)
                with mock.patch.object(sys, "meta_path", [Boom(), *sys.meta_path]):
                    out = config.discover_backends({"backends": {broken: {"type": broken},
                                                                 other: {"type": other}}})
                self.assertIn(other, out)
                self.assertIn(f"bad constant in {broken}", config.failed_backends[broken])
                self.assertNotIn("unknown backend type", config.failed_backends[broken])

    def test_a_broken_backend_does_not_take_the_others_down(self):
        class _B:
            capabilities: ClassVar[set[str]] = {"read"}

        def fake(name, bcfg):
            if name == "bad":
                raise RuntimeError("playwright is not installed")
            return _B()

        config.failed_backends.clear()
        with mock.patch.object(config, "_make_backend", fake):
            out = config.discover_backends({"backends": {"good": {}, "bad": {}}})
        self.assertIn("good", out)
        self.assertIn("playwright", config.failed_backends["bad"])

    def test_registry_contains_douban(self):
        self.assertIn("douban", config._registry())

    def test_a_configured_backend_that_failed_does_not_fall_back_silently(self):
        # `get` on a broken section must not hand back a default instance that ignores it.
        class _B:
            capabilities: ClassVar[set[str]] = {"read"}

        def fake(name, bcfg):
            if "cookie_file" in bcfg:
                raise RuntimeError("cannot read cookie_file")
            return _B()

        config.failed_backends.clear()
        with mock.patch.object(config, "_make_backend", fake):
            config.discover_backends({"backends": {"sd": {"type": "sciencedirect",
                                                          "cookie_file": "/nope"}}})
            with self.assertRaises(RuntimeError) as ctx:
                config.get_backend("sd", config={"backends": {"sd": {"type": "sciencedirect",
                                                                     "cookie_file": "/nope"}}})
        self.assertIn("cookie_file", str(ctx.exception))

    def test_get_backend_falls_back_to_registry(self):
        # A registered-but-unconfigured optional backend (douban) can be requested directly,
        # without being enabled in config: `search --backend douban` works out of the box.
        be = config.get_backend("douban", config={"backends": {"zotero": {"type": "zotero_local"}}})
        self.assertEqual(be.name, "douban")
        self.assertEqual(sorted(be.capabilities), ["read"])

    def test_registry_contains_open_access_backends(self):
        reg = config._registry()
        for name in ("doaj", "europepmc", "doab", "zenodo"):
            self.assertIn(name, reg)

    def test_open_access_backends_are_read_only(self):
        for name in ("doaj", "europepmc", "doab", "zenodo"):
            be = config.get_backend(name, config={"backends": {"zotero": {"type": "zotero_local"}}})
            self.assertEqual(sorted(be.capabilities), ["read"], name)

    def test_arxiv_field_passthrough(self):
        backends = config.discover_backends({"backends": {"a": {"type": "arxiv", "max_results": 5}}})
        self.assertEqual(backends["a"].name, "a")

    def test_write_backend_key_creates_skeleton(self):
        # When the file is missing, write the built-in default skeleton first, then inject api_key into
        # the zotero section
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.unlink(missing_ok=True)
        config.write_backend_key("zotero", "KEY1", path=cfg)
        text = cfg.read_text()
        self.assertIn("[backends.zotero]", text)
        self.assertIn("api_key = \"KEY1\"", text)
        # The skeleton should keep the default backends, so write_backend_key doesn't drop the other backends
        self.assertIn("[backends.arxiv]", text)
        self.assertIn("[backends.crossref]", text)
        self.assertIn("[backends.openalex]", text)

    def test_write_backend_key_upserts_existing(self):
        # Existing zotero section already has api_key: replace rather than duplicate
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\ntype = \"zotero_local\"\nbase = \"http://x\"\n"
                       "api_key = \"OLD\"\n\n[backends.arxiv]\ntype = \"arxiv\"\n", encoding="utf-8")
        config.write_backend_key("zotero", "NEW", path=cfg)
        text = cfg.read_text()
        self.assertIn("api_key = \"NEW\"", text)
        self.assertNotIn("api_key = \"OLD\"", text)
        # Other sections and type fields untouched
        self.assertIn("type = \"zotero_local\"", text)
        self.assertIn("[backends.arxiv]", text)

    def test_write_backend_key_appends_new_section(self):
        # No section for this backend in the file: append a [backends.<name>] section
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\ntype = \"zotero_local\"\n", encoding="utf-8")
        config.write_backend_key("uspto", "KEY2", path=cfg)
        text = cfg.read_text()
        self.assertIn("[backends.uspto]", text)
        self.assertIn("api_key = \"KEY2\"", text)
        # Original zotero section preserved
        self.assertIn("[backends.zotero]", text)

    def test_write_backend_key_quotes_safely(self):
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\n", encoding="utf-8")
        config.write_backend_key("zotero", 'a"b\\c', path=cfg)
        self.assertIn("api_key = \"a\\\"b\\\\c\"", cfg.read_text())

    def test_write_backend_key_escapes_control_characters(self):
        # A raw tab/newline in a basic string is invalid TOML; the file must still parse back.
        import tomllib

        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\n", encoding="utf-8")
        key = 'a\tb\nc\rd\x7fé'
        config.write_backend_key("zotero", key, path=cfg)
        self.assertNotIn("\n", cfg.read_text().split("api_key = ", 1)[1].splitlines()[0])
        self.assertEqual(tomllib.loads(cfg.read_text())["backends"]["zotero"]["api_key"], key)

    def test_a_missing_explicit_path_is_loud(self):
        # It used to fall back to the defaults while ``write_backend_key`` wrote the typo'd
        # path, so a mistyped LITCLI_CONFIG looked like it worked until the next write.
        missing = Path(tempfile.mkdtemp()) / "nope.toml"
        with self.assertRaises(BadInput) as caught:
            config.load_config(missing)
        self.assertIn(str(missing), str(caught.exception))

    def test_a_missing_env_path_says_where_it_came_from(self):
        missing = Path(tempfile.mkdtemp()) / "nope.toml"
        with mock.patch.dict(os.environ, {config.CONFIG_ENV: str(missing)}), \
                self.assertRaises(BadInput) as caught:
            config.load_config()
        self.assertIn("LITCLI_CONFIG", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "chmod only clears the read-only bit on Windows")
    def test_defaults_do_not_leak_between_loads(self):
        empty = Path(tempfile.mkdtemp())  # no config.toml: the built-in default answers
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(empty)}):
            os.environ.pop(config.CONFIG_ENV, None)
            first = config.load_config()
            self.assertIn("zotero", first["backends"])
            first["backends"]["zotero"]["api_key"] = "MUTATED"
            second = config.load_config()
        self.assertNotIn("api_key", second["backends"]["zotero"])

    @unittest.skipIf(os.name == "nt", "XDG_CONFIG_HOME is POSIX-only")
    def test_xdg_config_home_set_after_import_moves_the_default(self):
        # tempfile.mkdtemp never happens to exist before the call, so the default must be
        # resolved per call rather than frozen at import time.
        d = Path(tempfile.mkdtemp())
        (d / "litcli").mkdir()
        (d / "litcli" / "config.toml").write_text("[backends.arxiv]\ntype = \"arxiv\"\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(d)}):
            os.environ.pop(config.CONFIG_ENV, None)
            self.assertEqual(config.load_config(), {"backends": {"arxiv": {"type": "arxiv"}}})
            target = config.write_backend_key("zotero", "K")
        self.assertEqual(target, d / "litcli" / "config.toml")
        self.assertIn("[backends.zotero]", (d / "litcli" / "config.toml").read_text())

    @unittest.skipIf(os.name == "nt", "chmod only clears the read-only bit on Windows")
    def test_write_backend_key_restricts_permissions(self):
        # The file holds a credential; it must be owner-only regardless of the umask.
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\n", encoding="utf-8")
        os.chmod(cfg, 0o644)
        config.write_backend_key("zotero", "KEY", path=cfg)
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
