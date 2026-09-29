"""聚合领域层单测:归一化/身份键/去重/合并/排序。"""
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.models import AcademicItem
from litcli.aggregate import norm_title, norm_doi, identity, merge_items


class NormTest(unittest.TestCase):
    def test_norm_title(self):
        self.assertEqual(norm_title("  L u- VED  "), "l u ved")
        self.assertEqual(norm_title("Hello,  World!"), "hello world")
        self.assertEqual(norm_title(""), "")

    def test_norm_doi(self):
        self.assertEqual(norm_doi("10.1/ABc."), "10.1/abc")
        self.assertEqual(norm_doi("  "), "")

    def test_identity_prefers_doi(self):
        a = AcademicItem(source="z", key="k", title="X", doi="10.1/a")
        b = AcademicItem(source="a", key="k2", title="Totally different title", doi="10.1/a")
        self.assertEqual(identity(a), identity(b))
        self.assertEqual(identity(a), "doi:10.1/a")

    def test_identity_title_fallback(self):
        a = AcademicItem(source="z", key="k", title="Hello  World!", doi="")
        b = AcademicItem(source="a", key="k2", title="hello world", doi="")
        self.assertEqual(identity(a), identity(b))


class MergeTest(unittest.TestCase):
    def test_dedup_and_merge_sources(self):
        a = AcademicItem(source="zotero", key="k1", title="LU-ved", doi="10.1/ABc.",
                         year="2002", abstract="abs", url="http://x")
        b = AcademicItem(source="arxiv", key="1701.00001", title="Lu-ved", doi="10.1/abc", year="2002")
        out = merge_items([a, b])
        self.assertEqual(len(out), 1)
        # 保留主记录原始 DOI 快照,不重写
        self.assertEqual(out[0].doi, "10.1/ABc.")
        # 信息较全者(a)为主,缺失字段被补全
        self.assertEqual(out[0].abstract, "abs")
        self.assertEqual(out[0].url, "http://x")
        # 来源合并为字母序集合
        self.assertEqual(out[0].extra["sources"], ["arxiv", "zotero"])

    def test_title_dedup_without_doi(self):
        c = AcademicItem(source="zotero", key="k2", title="Hello  World!", year="2020")
        d = AcademicItem(source="arxiv", key="9999.9999", title="hello world", year="2020")
        out = merge_items([c, d])
        self.assertEqual(len(out), 1)

    def test_year_desc_then_title_asc(self):
        c = AcademicItem(source="z", key="c", title="Alpha", year="2020")
        e = AcademicItem(source="x", key="e", title="Newer", year="2024")
        out = merge_items([c, e])
        self.assertEqual(out[0].year, "2024")
        # 同年按标题升序
        f = AcademicItem(source="y", key="f", title="Zeta", year="2020")
        out2 = merge_items([c, f])
        self.assertEqual([x.title for x in out2], ["Alpha", "Zeta"])

    def test_does_not_mutate_input(self):
        a = AcademicItem(source="z", key="k1", title="T", doi="10.1/a")
        b = AcademicItem(source="a", key="k2", title="T", doi="10.1/a")
        items = [a, b]
        out = merge_items(items)
        self.assertEqual(len(items), 2)  # 入参未被合并
        self.assertEqual(a.doi, "10.1/a")
        self.assertIsNot(out[0], a)


if __name__ == "__main__":
    unittest.main()
