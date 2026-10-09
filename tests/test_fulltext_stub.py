"""fulltext 合法 OA 通道桩测:注入解析器,断言优先级/被墙标注/单步失败不拖垮。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import fulltext as ft


def make_resolver(value):
    def r(doi):
        return value
    return r


class FulltextStubTest(unittest.TestCase):
    def test_priority_first_resolver_wins(self):
        cand = {"kind": "unpaywall", "url": "http://oa.example/p.pdf", "title": "T"}
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            make_resolver(cand), make_resolver({"kind": "openalex", "url": "http://2.pdf"}),
        ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "unpaywall")
        self.assertEqual(out["url"], "http://oa.example/p.pdf")

    def test_second_resolver_used_when_first_none(self):
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            make_resolver(None), make_resolver({"kind": "openalex", "url": "http://2.pdf"}),
        ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "openalex")

    def test_first_resolver_raises_skipped(self):
        def boom(d):
            raise RuntimeError("network down")
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            boom, make_resolver({"kind": "openalex", "url": "http://2.pdf"}),
        ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "openalex")

    def test_no_fulltext_flagged(self):
        out = ft.resolve_fulltext("10.1/x", resolvers=[make_resolver(None)])
        self.assertFalse(out["has_fulltext"])

    def test_empty_url_not_accepted(self):
        out = ft.resolve_fulltext("10.1/x", resolvers=[
            make_resolver({"kind": "unpaywall", "url": ""}),
        ])
        self.assertFalse(out["has_fulltext"])

    def test_local_beats_oa(self):
        """本地库命中的附件优先于 OA 直链(用户要求:本地优先)。"""
        class FakeBackend:
            name = "zotero"
            def can(self, cap):
                return cap == "attachment"

            def search(self, q, limit=10):
                return [type("I", (), {"key": "K1", "doi": q, "url": "", "title": "T"})]
            def get_attachment(self, key):
                return b"%PDF-1.4 local"
        out = ft.resolve_fulltext("10.1/x", backends={"zotero": FakeBackend()},
                                 resolvers=[
                                     lambda d: ft._from_local(d, {"zotero": FakeBackend()}),
                                     lambda d: {"kind": "openalex", "url": "http://oa.pdf"},
                                 ])
        self.assertTrue(out["has_fulltext"])
        self.assertEqual(out["kind"], "local")
        self.assertIn(b"%PDF", out["data"])

    def test_from_local_skips_backend_without_attachment(self):
        class NoAttach:
            name = "arxiv"
            def can(self, cap):
                return False  # 无 attachment 能力,跳过
        self.assertIsNone(ft._from_local("10.1/x", {"arxiv": NoAttach()}))


if __name__ == "__main__":
    unittest.main()
