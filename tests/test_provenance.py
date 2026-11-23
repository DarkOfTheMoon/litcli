"""Provenance labels: every hit says what kind of copy it is, and the label travels with it."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import config, provenance
from litcli.commands.render import present
from litcli.models import AcademicItem


class LabelTest(unittest.TestCase):
    def test_labels_read_as_a_sentence_fragment(self):
        self.assertEqual(provenance.label("oa"), "open access")
        self.assertEqual(provenance.label("public"), "public copy")
        self.assertEqual(provenance.label("local"), "your library")

    def test_an_unknown_access_has_no_label_or_tag(self):
        self.assertEqual(provenance.label(""), "")
        self.assertEqual(provenance.tag(""), "")

    def test_tags_are_compact(self):
        self.assertEqual(provenance.tag("public"), "[public]")


class StampTest(unittest.TestCase):
    class Backend:
        access = "oa"

    class Metadata:
        access = ""

    def test_items_take_the_backend_access(self):
        items = [AcademicItem(source="arxiv", key="1"), AcademicItem(source="arxiv", key="2")]
        self.assertIs(provenance.stamp(items, self.Backend()), items)
        self.assertEqual([i.access for i in items], ["oa", "oa"])

    def test_a_metadata_backend_leaves_items_alone(self):
        item = AcademicItem(source="crossref", key="10.1/x")
        provenance.stamp([item], self.Metadata())
        self.assertEqual(item.access, "")

    def test_an_access_the_item_already_carries_is_kept(self):
        # A backend can answer per item: a SpringerLink open-access article is OA even though
        # the backend as a whole is an entitled one.
        oa = AcademicItem(source="springer", key="doi:10.1038/x", access=provenance.OA)
        plain = AcademicItem(source="springer", key="doi:10.1007/y")
        provenance.stamp([oa, plain], self.Backend())
        self.assertEqual((oa.access, plain.access), ("oa", "oa"))


class RegistryTest(unittest.TestCase):
    """The declaration lives on the backend, so a renamed or wrapped backend keeps it."""

    def test_known_access_values(self):
        expected = {"zotero_local": "local", "scihub": "public", "zlibrary": "public",
                    "arxiv": "oa", "doab": "oa", "europepmc": "oa", "zenodo": "oa",
                    "doaj": "oa", "openalex": "oa"}
        registry = config._registry()
        for name, access in expected.items():
            self.assertEqual(getattr(registry[name], "access", None), access, name)

    def test_metadata_only_backends_declare_nothing(self):
        registry = config._registry()
        for name in ("crossref", "openlibrary", "uspto"):
            self.assertEqual(getattr(registry[name], "access", ""), "", name)


class LegendTest(unittest.TestCase):
    """A public copy is explained once per run; the reader, not the row, owes the licence check."""

    def test_the_note_is_printed_once_for_public_items(self):
        import contextlib
        import io

        from litcli.commands.render import print_items

        items = [AcademicItem(source="scihub", key="a", title="T", access="public"),
                 AcademicItem(source="zlibrary", key="b", title="T", access="public")]
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            print_items(items, "table", True, "[no results]")
        self.assertEqual(err.getvalue().count("[note]"), 1)
        self.assertIn("not an open-access licence", err.getvalue())
        self.assertNotIn("[note]", out.getvalue())

    def test_no_note_without_public_items(self):
        import contextlib
        import io

        from litcli.commands.render import print_items

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            print_items([AcademicItem(source="arxiv", key="1", title="T", access="oa")],
                        "table", True, "[no results]")
        self.assertEqual(err.getvalue(), "")


class RenderTest(unittest.TestCase):
    def test_the_table_marks_the_access(self):
        item = AcademicItem(source="zlibrary", key="book/x", title="T", access="public")
        self.assertIn("[public]", present(item))
        self.assertNotIn(item.title, present(item, no_cite=False).split("[public]")[0])

    def test_a_metadata_item_has_no_marker(self):
        item = AcademicItem(source="crossref", key="10.1/x", title="T")
        self.assertNotIn("[oa]", present(item))

    def test_json_carries_the_access_field(self):
        item = AcademicItem(source="arxiv", key="1", title="T", access="oa")
        self.assertEqual(json.loads(present(item, "json"))["access"], "oa")


if __name__ == "__main__":
    unittest.main()
