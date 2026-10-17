"""Backend parse-layer (zotero / arxiv) pure-function unit tests: constructed data, no real library, no network."""
import unittest
import xml.etree.ElementTree as ET

from litcli.backends import zotero_local, arxiv


class TestZoteroParse(unittest.TestCase):
    def setUp(self):
        self.data = {
            "key": "K1", "itemType": "journalArticle", "title": "T",
            "creators": [{"firstName": "J", "lastName": "Smith"}],
            "date": "2002", "abstractNote": "abs", "DOI": "10.x",
            "url": "http://u", "version": 7,
        }

    def test_item_to_academic(self):
        it = zotero_local._item_to_academic(self.data)
        self.assertEqual(it.year, "2002")
        self.assertEqual(it.creators[0]["family"], "Smith")
        self.assertEqual(it.item_type, "article")
        self.assertEqual(it.abstract, "abs")

    def test_first_digit_year(self):
        self.assertEqual(zotero_local.first_digit_year("2020-03-12"), "2020")
        self.assertEqual(zotero_local.first_digit_year("2002"), "2002")
        self.assertEqual(zotero_local.first_digit_year("n/a"), "")

    def test_to_zotero_json(self):
        it = zotero_local._item_to_academic(self.data)
        j = zotero_local.ZoteroLocalBackend._to_zotero_json(it)
        self.assertEqual(j["itemType"], "journalArticle")
        self.assertEqual(j["date"], "2002")
        self.assertEqual(j["creators"][0]["lastName"], "Smith")


class TestArxivParse(unittest.TestCase):
    XML = ("""<entry xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
        <id>http://arxiv.org/abs/2006.12345v1</id><title>A Title</title>
        <summary>Some abstract here</summary><published>2020-06-15T00:00:00Z</published>
        <author><name>John Smith</name></author><author><name>Jane Doe</name></author>
        <arxiv:doi>10.1000/xyz</arxiv:doi></entry>""")

    def test_entry_to_item(self):
        e = ET.fromstring(self.XML)
        it = arxiv._entry_to_item(e)
        self.assertEqual(it.key, "2006.12345")
        self.assertEqual(it.title, "A Title")
        self.assertEqual(it.creators[0]["given"], "John")
        self.assertEqual(it.creators[0]["family"], "Smith")
        self.assertEqual(it.year, "2020")
        self.assertEqual(it.doi, "10.1000/xyz")

    def test_short_id_strips_version(self):
        self.assertEqual(arxiv._short_id("http://arxiv.org/abs/2006.12345v3"), "2006.12345")


if __name__ == "__main__":
    unittest.main()
