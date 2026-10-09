"""config 后端发现单测:直接传 dict,不落盘、不联网。"""
import tempfile
import unittest
from pathlib import Path

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

    def test_arxiv_field_passthrough(self):
        backends = config.discover_backends({"backends": {"a": {"type": "arxiv", "max_results": 5}}})
        self.assertEqual(backends["a"].name, "a")

    def test_write_backend_key_creates_skeleton(self):
        # 缺失文件时先落内置默认骨架,再插 api_key 到 zotero 段
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.unlink(missing_ok=True)
        config.write_backend_key("zotero", "KEY1", path=cfg)
        text = cfg.read_text()
        self.assertIn("[backends.zotero]", text)
        self.assertIn("api_key = \"KEY1\"", text)
        # 骨架应保留默认后端,避免 write_backend_key 后丢其它后端
        self.assertIn("[backends.arxiv]", text)
        self.assertIn("[backends.crossref]", text)
        self.assertIn("[backends.openalex]", text)

    def test_write_backend_key_upserts_existing(self):
        # 已有 zotero 段且带 api_key:替换而非重复
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\ntype = \"zotero_local\"\nbase = \"http://x\"\napi_key = \"OLD\"\n\n[backends.arxiv]\ntype = \"arxiv\"\n", encoding="utf-8")
        config.write_backend_key("zotero", "NEW", path=cfg)
        text = cfg.read_text()
        self.assertIn("api_key = \"NEW\"", text)
        self.assertNotIn("api_key = \"OLD\"", text)
        # 未动其它段与类型字段
        self.assertIn("type = \"zotero_local\"", text)
        self.assertIn("[backends.arxiv]", text)

    def test_write_backend_key_appends_new_section(self):
        # 文件中无该后端段:追加一个 [backends.<name>] 段
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\ntype = \"zotero_local\"\n", encoding="utf-8")
        config.write_backend_key("uspto", "KEY2", path=cfg)
        text = cfg.read_text()
        self.assertIn("[backends.uspto]", text)
        self.assertIn("api_key = \"KEY2\"", text)
        # 原 zotero 段保留
        self.assertIn("[backends.zotero]", text)

    def test_write_backend_key_quotes_safely(self):
        d = Path(tempfile.mkdtemp())
        cfg = d / "config.toml"
        cfg.write_text("[backends.zotero]\n", encoding="utf-8")
        config.write_backend_key("zotero", 'a"b\\c', path=cfg)
        self.assertIn("api_key = \"a\\\"b\\\\c\"", cfg.read_text())


if __name__ == "__main__":
    unittest.main()
