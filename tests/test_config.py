"""Config backend-discovery unit tests: pass a dict directly, no disk write, no network."""
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from litcli import config


class TestConfig(unittest.TestCase):
    def test_discover_inbuilt(self):
        backends = config.discover_backends(
            {"backends": {"zotero": {"type": "zotero_local", "base": "http://x"}}})
        self.assertIn("zotero", backends)
        self.assertEqual(sorted(backends["zotero"].capabilities),
                         ["attachment", "read", "write"])

    def test_unknown_type_raises(self):
        with self.assertRaises(KeyError):
            config.discover_backends({"backends": {"x": {"type": "nope"}}})

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
