"""CLI 助手函数桩测:arXiv key 归一化 + backends 子命令面 + search --type。

不联网;只测纯函数与 argparse 面(不实例化后端/不发起请求)。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.cli import _arxiv_id_from_key, _normalize_export_key, _normalize_related_key, _build_parser


class ArxivKeyNormalizeTest(unittest.TestCase):
    def test_arxiv_id_from_prefixed_forms(self):
        self.assertEqual(_arxiv_id_from_key("arxiv/1806.09180"), "1806.09180")
        self.assertEqual(_arxiv_id_from_key("arXiv:1806.09180"), "1806.09180")
        self.assertEqual(_arxiv_id_from_key("arxiv/1806.09180v1"), "1806.09180")

    def test_arxiv_id_from_non_arxiv(self):
        self.assertIsNone(_arxiv_id_from_key("10.1/abc"))
        self.assertIsNone(_arxiv_id_from_key("10.48550/arxiv.1806.09180"))
        self.assertIsNone(_arxiv_id_from_key(""))

    def test_normalize_for_arxiv_backend_uses_bare_id(self):
        self.assertEqual(_normalize_export_key("arxiv/1806.09180", "arxiv"), "1806.09180")

    def test_normalize_for_other_backend_uses_doi(self):
        # 非 arxiv 后端(openalex/crossref)需 DOI 形态才能解析,否则被当 OpenAlex id 查而 404
        self.assertEqual(_normalize_export_key("arxiv/1806.09180", "openalex"),
                         "10.48550/arxiv.1806.09180")

    def test_non_arxiv_key_passthrough(self):
        self.assertEqual(_normalize_export_key("10.1/abc", "crossref"), "10.1/abc")


class RelatedKeyNormalizeTest(unittest.TestCase):
    def test_strips_source_prefix(self):
        self.assertEqual(_normalize_related_key("openalex/10.1016/j.cma.2004.11.019"),
                         "10.1016/j.cma.2004.11.019")
        self.assertEqual(_normalize_related_key("zotero/AB12"), "AB12")

    def test_arxiv_prefix_to_doi(self):
        self.assertEqual(_normalize_related_key("arxiv/1806.09180"), "10.48550/arxiv.1806.09180")

    def test_openalex_url_and_doi_passthrough(self):
        # extra["openalex_id"] 形态(CLI 自带)原样交付;裸 DOI 亦不动
        self.assertEqual(_normalize_related_key("https://openalex.org/W2919115771"),
                         "https://openalex.org/W2919115771")
        self.assertEqual(_normalize_related_key("10.48550/arxiv.1806.09180"),
                         "10.48550/arxiv.1806.09180")


class BackendsParserTest(unittest.TestCase):
    def test_backends_accepts_list_and_status(self):
        p = _build_parser()
        for cmd in ("list", "status"):
            a = p.parse_args(["backends", cmd])
            self.assertEqual(a.backends_cmd, cmd)
        a = p.parse_args(["backends"])
        self.assertIsNone(a.backends_cmd)
        a = p.parse_args(["backends", "authorize"])
        self.assertEqual(a.backends_cmd, "authorize")

    def test_search_accepts_type_filter(self):
        a = _build_parser().parse_args(["search", "q", "--type", "article"])
        self.assertEqual(a.type, "article")

    def test_search_type_defaults_none(self):
        a = _build_parser().parse_args(["search", "q"])
        self.assertIsNone(a.type)


if __name__ == "__main__":
    unittest.main()
