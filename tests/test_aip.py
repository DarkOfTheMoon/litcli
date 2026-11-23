"""AIP backend offline tests: pure parsers and the stubbed fetch path."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.aip import (
    AipBackend,
    extract_pdf_url,
    is_block_page,
    is_open_access,
    normalize_key,
    page_to_item,
    parse_cookie,
)
from litcli.errors import BadInput, NotFound, Unavailable
from tests.http_stub import speed_up, stub

DOI = "10.1063/1.870872"
ID = "260756"
URL = (f"https://pubs.aip.org/pop/article/1/12/4022/{ID}/"
       "Local-modes-of-field-reversed-configurations")
LEGACY = f"https://aip.scitation.org/doi/abs/{DOI}"
PDF = f"https://pubs.aip.org/pop/article-pdf/1/12/4022/{ID}/12345678/4022_1_online.pdf"

CITATION_PAGE = f"""<html><head>
<meta name="citation_title" content="Meta Carried Title">
<meta name="citation_author" content="Doe, Jane">
<meta name="citation_journal_title" content="Physics of Fluids">
<meta name="citation_volume" content="7">
<meta name="citation_firstpage" content="55">
<meta name="citation_lastpage" content="66">
<meta name="citation_publication_date" content="1995/03/01">
<meta name="citation_doi" content="{DOI}">
<meta name="citation_pdf_url" content="{PDF}">
</head><body></body></html>"""

DC_PAGE = """<html><head>
<meta name="dc.title" content="Dublin Core Title">
<meta name="dc.creator" content="Roe, Richard">
<meta name="dc.source" content="DC Journal">
<meta name="dc.date" content="2018">
<meta name="dc.identifier" content="10.1063/1.000001">
</head><body></body></html>"""

#: The licence block an open-access page really carries (the wording, inside its own class).
LICENCE = ('<div class="creative-commons__description">This is an Open Access article, '
           'distributed under the terms of the Creative Commons Attribution licence</div>')
OA_PAGE = CITATION_PAGE.replace("</head>", f"{LICENCE}</head>")

BLOCK_PAGE = """<html><head><title>Just a moment...</title></head><body>
<div id="cf-challenge"><p>Enable JavaScript and cookies to continue</p></div></body></html>"""


class NormalizeKeyTest(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(normalize_key(DOI), f"doi:{DOI}")

    def test_aip_prefix(self):
        self.assertEqual(normalize_key(f"aip:{DOI}"), f"doi:{DOI}")

    def test_source_prefix_form_is_accepted(self):
        self.assertEqual(normalize_key(f"aip/{DOI}"), f"doi:{DOI}")

    def test_doi_url(self):
        self.assertEqual(normalize_key(f"https://doi.org/{DOI}"), f"doi:{DOI}")

    def test_a_legacy_doi_url_yields_the_doi(self):
        self.assertEqual(normalize_key(LEGACY), f"doi:{DOI}")

    def test_a_legacy_pdf_url_yields_the_doi(self):
        self.assertEqual(normalize_key(f"https://aip.scitation.org/doi/pdf/{DOI}"), f"doi:{DOI}")

    def test_a_modern_url_stays_a_url(self):
        # The modern path carries an internal id, not the DOI.
        self.assertEqual(normalize_key(URL), f"url:{URL}")

    def test_a_query_tail_is_kept_and_a_fragment_dropped(self):
        self.assertEqual(normalize_key(f"{URL}?x=1#frag"), f"url:{URL}?x=1")

    def test_a_prefixed_url_is_accepted(self):
        self.assertEqual(normalize_key(f"aip:{URL}"), f"url:{URL}")

    def test_a_bare_host_is_accepted(self):
        self.assertEqual(normalize_key("https://pubs.aip.org"), "url:https://pubs.aip.org")

    def test_an_already_normalized_url_key_round_trips(self):
        self.assertEqual(normalize_key(f"url:{URL}"), f"url:{URL}")

    def test_other_hosts_are_rejected(self):
        with self.assertRaises(BadInput):
            normalize_key("https://www.cambridge.org/core/x")

    def test_empty_key(self):
        with self.assertRaises(BadInput):
            normalize_key("  ")

    def test_garbage(self):
        with self.assertRaises(BadInput):
            normalize_key("hello world")


class ExtractPdfUrlTest(unittest.TestCase):
    def test_citation_meta_wins(self):
        self.assertEqual(extract_pdf_url(CITATION_PAGE), PDF)

    def test_an_anchor_alone_is_found(self):
        page = f'<a href="/pop/article-pdf/1/12/4022/{ID}/12345678/4022_1_online.pdf">PDF</a>'
        self.assertEqual(extract_pdf_url(page), PDF)

    def test_an_anchor_without_the_journal_segment_resolves_too(self):
        page = f'<a href="/article-pdf/1/12/4022/{ID}/12345678/4022_1_online.pdf">PDF</a>'
        self.assertEqual(extract_pdf_url(page),
                         f"https://pubs.aip.org/article-pdf/1/12/4022/{ID}/12345678/"
                         "4022_1_online.pdf")

    def test_the_doi_is_a_last_resort(self):
        self.assertEqual(extract_pdf_url("<html></html>", DOI),
                         f"https://pubs.aip.org/doi/pdf/{DOI}")

    def test_no_link_and_no_doi(self):
        self.assertEqual(extract_pdf_url("<html></html>"), "")

    def test_a_related_card_link_is_not_taken(self):
        # Related cards carry the same shape; the wanted id picks the article's own link.
        other = "/pop/article-pdf/1/12/4022/999999/12345678/4022_1_online.pdf"
        page = f'<a href="/{other.lstrip(chr(47))}">other</a>'
        self.assertEqual(extract_pdf_url(page + f'<a href="{PDF}">own</a>', "", ID), PDF)
        # With no matching anchor at all the first one is still better than nothing.
        self.assertTrue(extract_pdf_url(page, "", ID).endswith("/999999/12345678/"
                                                              "4022_1_online.pdf"))

    def test_an_off_host_link_raises(self):
        page = '<meta name="citation_pdf_url" content="https://evil.example.com/x.pdf">'
        with self.assertRaises(RuntimeError):
            extract_pdf_url(page)


class PageToItemTest(unittest.TestCase):
    def test_citation_meta(self):
        item = page_to_item(CITATION_PAGE, f"doi:{DOI}", URL)
        self.assertEqual(item.title, "Meta Carried Title")
        self.assertEqual(item.container_title, "Physics of Fluids")
        self.assertEqual(item.page, "55-66")
        self.assertEqual(item.year, "1995")
        self.assertEqual(item.doi, DOI)
        self.assertEqual(item.item_type, "article")

    def test_a_dc_only_page_still_parses(self):
        self.assertEqual(page_to_item(DC_PAGE, "url:x").title, "Dublin Core Title")

    def test_a_title_tag_is_the_last_resort(self):
        item = page_to_item("<html><head><title>Tag Title | AIP Publishing</title></head></html>",
                            "url:x")
        self.assertEqual(item.title, "Tag Title")

    def test_the_page_doi_wins_over_the_key(self):
        self.assertEqual(page_to_item(CITATION_PAGE, f"url:{URL}", URL).doi, DOI)


class MarkerTest(unittest.TestCase):
    def test_a_challenge_page(self):
        self.assertTrue(is_block_page(BLOCK_PAGE, 403))
        self.assertTrue(is_block_page("", 403))
        self.assertFalse(is_block_page(CITATION_PAGE, 200))

    def test_only_aip_cookie_rows_are_kept(self):
        raw = ("pubs.aip.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tabc\n"
               ".example.com\tTRUE\t/\tTRUE\t0\tx\ty")
        self.assertEqual(parse_cookie(raw), {"cf_clearance": "abc"})


class OpenAccessMarkerTest(unittest.TestCase):
    def test_the_licence_block_is_open_access(self):
        self.assertTrue(is_open_access(OA_PAGE))

    def test_a_paywalled_page_is_not(self):
        self.assertFalse(is_open_access(CITATION_PAGE))

    def test_a_bare_cc_link_from_a_related_card_is_not_open_access(self):
        page = CITATION_PAGE.replace(
            "</head>", '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY</a></head>')
        self.assertFalse(is_open_access(page))


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.be = AipBackend()
        speed_up(self.be)

    def test_capabilities_are_read_only(self):
        self.assertEqual(self.be.capabilities, {"read"})

    def test_access_is_institutional_not_oa(self):
        self.assertEqual(self.be.access, "entitled")

    def test_a_url_key_fetches_that_page(self):
        rec = stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(f"url:{URL}").title, "Meta Carried Title")
        self.assertEqual(rec.urls, [URL])

    def test_a_doi_goes_through_doi_org(self):
        rec = stub(self.be, (302, "", {"location": URL}), (200, CITATION_PAGE))
        self.be.get(DOI)
        self.assertTrue(rec.urls[0].startswith("https://doi.org/"))
        self.assertEqual(rec.urls[-1], URL)

    def test_get_puts_the_pdf_url_in_attachments(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(f"url:{URL}").attachments, [PDF])

    def test_a_url_key_becomes_a_doi_key_once_the_page_names_it(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(f"url:{URL}").key, f"doi:{DOI}")

    def test_an_open_access_page_marks_the_item(self):
        stub(self.be, (200, OA_PAGE))
        item = self.be.get(f"url:{URL}")
        self.assertTrue(item.extra.get("open_access"))
        self.assertEqual(item.access, "oa")

    def test_a_paywalled_page_is_not_marked_open_access(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertFalse(self.be.get(f"url:{URL}").extra.get("open_access"))

    def test_a_page_without_a_pdf_link_falls_back_to_the_doi_rewrite(self):
        rec = stub(self.be, (302, "", {"location": URL}), (200, DC_PAGE))
        item = self.be.get(DOI)
        # The page's own DOI (from its meta) is what the rewrite uses.
        self.assertEqual(item.attachments, ["https://pubs.aip.org/doi/pdf/10.1063/1.000001"])
        self.assertEqual(len(rec.urls), 2)

    def test_a_page_without_a_pdf_link_or_doi_has_no_attachments(self):
        stub(self.be, (200, "<html><head><title>T</title></head></html>"))
        self.assertEqual(self.be.get(f"url:{URL}").attachments, [])

    def test_404_is_not_found(self):
        stub(self.be, (404, "<html>not found</html>"))
        with self.assertRaises(NotFound):
            self.be.get(DOI)

    def test_a_doi_landing_off_aip_is_not_found(self):
        stub(self.be, (302, "", {"location": "https://example.org/x"}), (200, CITATION_PAGE))
        with self.assertRaises(NotFound):
            self.be.get(DOI)

    def test_a_challenge_page_becomes_the_gate_message(self):
        stub(self.be, (403, BLOCK_PAGE))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.get(f"url:{URL}")
        self.assertIn("AIP Publishing", str(ctx.exception))
        self.assertIn("cookie_file", str(ctx.exception))

    def test_a_challenge_with_a_200_status_is_still_a_gate(self):
        stub(self.be, (200, BLOCK_PAGE))
        with self.assertRaises(RuntimeError):
            self.be.get(f"url:{URL}")

    def test_search_resolves_a_doi(self):
        stub(self.be, (302, "", {"location": URL}), (200, CITATION_PAGE))
        self.assertEqual(len(self.be.search(DOI)), 1)

    def test_search_on_a_word_returns_nothing_and_sends_nothing(self):
        rec = stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.search("local modes"), [])
        self.assertEqual(rec.urls, [])

    def test_search_not_found_returns_nothing(self):
        stub(self.be, (404, "x"))
        self.assertEqual(self.be.search(DOI), [])

    def test_get_attachment_returns_the_bytes(self):
        stub(self.be, (200, CITATION_PAGE), (200, b"%PDF-1.7 bytes"))
        self.assertTrue(self.be.get_attachment(f"url:{URL}").startswith(b"%PDF"))

    def test_an_html_answer_without_a_session_names_the_missing_cookie(self):
        stub(self.be, (200, CITATION_PAGE), (200, "<html>the article page</html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("holds no clearance cookie", str(ctx.exception))

    def test_an_html_answer_with_a_session_names_the_entitlement(self):
        be = AipBackend(cookie="cf_clearance=abc", user_agent="UA/1")
        speed_up(be)
        stub(be, (200, CITATION_PAGE), (200, "<html>the article page</html>"))
        with self.assertRaises(Unavailable) as ctx:
            be.get_attachment(f"url:{URL}")
        self.assertIn("not entitled", str(ctx.exception))

    def test_an_html_answer_on_an_open_access_page_says_so(self):
        stub(self.be, (200, OA_PAGE), (200, "<html>the article page</html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("open access", str(ctx.exception))

    def test_a_gated_pdf_download(self):
        stub(self.be, (200, CITATION_PAGE), (403, BLOCK_PAGE))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("gated", str(ctx.exception))

    def test_get_attachment_without_a_link_asks_for_a_doi(self):
        stub(self.be, (200, "<html><head><title>T</title></head></html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("no PDF link", str(ctx.exception))

    def test_a_promoted_url_key_shares_the_memo_with_its_doi(self):
        rec = stub(self.be, (200, CITATION_PAGE), (200, b"%PDF-1.7"))
        self.be.get(f"url:{URL}")
        self.be.get_attachment(f"doi:{DOI}")
        self.assertEqual(rec.urls, [URL, PDF])

    def test_the_page_is_fetched_once_for_get_plus_attachment(self):
        rec = stub(self.be, (200, CITATION_PAGE), (200, b"%PDF-1.7"))
        self.be.get(f"url:{URL}")
        self.be.get_attachment(f"url:{URL}")
        self.assertEqual(rec.urls, [URL, PDF])


class ConstructionTest(unittest.TestCase):
    def test_a_cookie_without_a_user_agent_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            AipBackend(cookie="a=1")
        self.assertIn("user_agent", str(ctx.exception))
        self.assertIn("AIP clearance cookie", str(ctx.exception))

    def test_a_cookie_pair_is_sent_with_every_request(self):
        be = AipBackend(cookie="cf_clearance=abc", user_agent="UA/1")
        speed_up(be)
        rec = stub(be, (200, CITATION_PAGE))
        be.get(f"url:{URL}")
        self.assertIn("cf_clearance=abc", rec.header("cookie"))
        self.assertEqual(rec.header("user-agent"), "UA/1")

    def test_has_session(self):
        self.assertFalse(AipBackend().has_session)
        self.assertTrue(AipBackend(cookie="a=1", user_agent="UA/1").has_session)


if __name__ == "__main__":
    unittest.main()
