"""config 后端发现单测:直接传 dict,不落盘、不联网。"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
