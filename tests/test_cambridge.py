"""Cambridge backend offline tests: pure parsers and the stubbed fetch path."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.cambridge import (
    CambridgeBackend,
    extract_pdf_url,
    is_block_page,
    is_open_access,
    is_short_page,
    normalize_key,
    page_to_item,
    parse_cookie,
)
from litcli.errors import BadInput, NotFound, Unavailable
from tests.http_stub import speed_up, stub

DOI = "10.1017/jfm.2016.10"
ID = "273B8416BB80A7AE23315B948FD7ACB9"
URL = (f"https://www.cambridge.org/core/journals/journal-of-fluid-mechanics/article/"
       f"cavity-formation/{ID}")
ABS_URL = (f"https://www.cambridge.org/core/journals/journal-of-fluid-mechanics/article/abs/"
           f"cavity-formation/{ID}")
PDF_PATH = (f"/core/services/aop-cambridge-core/content/view/{ID}/S0022112016000100a.pdf")
PDF = f"https://www.cambridge.org{PDF_PATH}"
PDF_LONG = f"{PDF}/div-class-title-cavity-formation-div.pdf"
CHAPTER_URL = "https://www.cambridge.org/core/books/strategy-as-practice/introducing/4CE52"

CITATION_PAGE = f"""<html><head>
<meta name="citation_title"  content="Meta Carried Title">
<meta name="citation_author"  content="Doe, Jane">
<meta name="citation_journal_title"  content="Journal of Fluid Mechanics">
<meta name="citation_volume"  content="7">
<meta name="citation_firstpage"  content="55">
<meta name="citation_lastpage"  content="66">
<meta name="citation_publication_date"  content="2016/02/01">
<meta name="citation_doi"  content="{DOI}">
<meta name="citation_pdf_url"  content="{PDF_LONG}">
</head><body></body></html>"""

CHAPTER_PAGE = """<html><head>
<meta name="citation_title"  content="A Chapter">
<meta name="citation_inbook_title"  content="The Book">
<meta name="citation_doi"  content="10.1017/CBO9780511618925.003">
</head><body></body></html>"""

DC_PAGE = """<html><head>
<meta name="dc.title" content="Dublin Core Title">
<meta name="dc.creator" content="Roe, Richard">
<meta name="dc.source" content="DC Journal">
<meta name="dc.date" content="2018">
<meta name="dc.identifier" content="10.1017/j.dc.2018.01.003">
</head><body></body></html>"""

#: The licence block an open-access page really carries (the wording, inside its own class).
LICENCE = ('<div class="creative-commons__description">This is an Open Access article, '
           'distributed under the terms of the Creative Commons Attribution licence'
           ' (<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>)</div>')
OA_PAGE = CITATION_PAGE.replace("</head>", f"{LICENCE}</head>")

BLOCK_PAGE = """<html><head><title>Just a moment...</title></head><body>
<div><p>Enable JavaScript and cookies to continue</p></div></body></html>"""


class NormalizeKeyTest(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(normalize_key(DOI), f"doi:{DOI}")

    def test_cambridge_prefix(self):
        self.assertEqual(normalize_key(f"cambridge:{DOI}"), f"doi:{DOI}")

    def test_source_prefix_form_is_accepted(self):
        # The table prints ``source/key``; a pasted key must resolve like ``cambridge:<DOI>``.
        self.assertEqual(normalize_key(f"cambridge/{DOI}"), f"doi:{DOI}")

    def test_doi_url(self):
        self.assertEqual(normalize_key(f"https://doi.org/{DOI}"), f"doi:{DOI}")

    def test_percent_encoded_doi(self):
        self.assertEqual(normalize_key("10.1017%2Fjfm.2016.10"), f"doi:{DOI}")

    def test_article_url_stays_a_url(self):
        self.assertEqual(normalize_key(URL), f"url:{URL}")

    def test_abs_url_stays_a_url(self):
        self.assertEqual(normalize_key(ABS_URL), f"url:{ABS_URL}")

    def test_a_query_tail_is_kept_and_a_fragment_dropped(self):
        self.assertEqual(normalize_key(f"{URL}?x=1#frag"), f"url:{URL}?x=1")

    def test_a_prefixed_url_is_accepted(self):
        self.assertEqual(normalize_key(f"cambridge:{URL}"), f"url:{URL}")

    def test_a_bare_host_is_accepted(self):
        self.assertEqual(normalize_key("https://www.cambridge.org"), "url:https://www.cambridge.org")

    def test_an_already_normalized_url_key_round_trips(self):
        self.assertEqual(normalize_key(f"url:{URL}"), f"url:{URL}")

    def test_other_hosts_are_rejected(self):
        with self.assertRaises(BadInput):
            normalize_key("https://onlinelibrary.wiley.com/doi/10.1002/x")

    def test_empty_key(self):
        with self.assertRaises(BadInput):
            normalize_key("  ")

    def test_garbage(self):
        with self.assertRaises(BadInput):
            normalize_key("hello world")


class ExtractPdfUrlTest(unittest.TestCase):
    def test_citation_meta_wins(self):
        self.assertEqual(extract_pdf_url(CITATION_PAGE), PDF)

    def test_the_descriptive_tail_is_trimmed(self):
        # Cambridge appends ``/div-class-title-…-div.pdf``; the short path serves the same bytes.
        page = f'<a href="{PDF_LONG}">PDF</a>'
        self.assertEqual(extract_pdf_url(page), PDF)

    def test_an_anchor_alone_is_found(self):
        self.assertEqual(extract_pdf_url(f'<a href="{PDF_PATH}">PDF</a>'), PDF)

    def test_a_protocol_relative_link(self):
        page = f'<meta name="citation_pdf_url" content="//www.cambridge.org{PDF_PATH}">'
        self.assertEqual(extract_pdf_url(page), PDF)

    def test_no_link(self):
        self.assertEqual(extract_pdf_url("<html></html>"), "")

    def test_a_related_card_link_is_not_taken(self):
        # Related cards carry the same shape; the wanted product id picks the item's own.
        other = "/core/services/aop-cambridge-core/content/view/" + "A" * 32 + "/S111a.pdf"
        page = f'<a href="{other}">other</a>'
        # A match wins; with no match at all the first anchor is still better than nothing.
        self.assertEqual(extract_pdf_url(page + f'<a href="{PDF_PATH}">own</a>', ID), PDF)
        self.assertTrue(extract_pdf_url(page, ID).endswith("/S111a.pdf"))
        self.assertEqual(extract_pdf_url(page), extract_pdf_url(page, ID))

    def test_an_off_host_link_raises(self):
        page = '<meta name="citation_pdf_url" content="https://evil.example.com/x.pdf">'
        with self.assertRaises(RuntimeError):
            extract_pdf_url(page)


class PageToItemTest(unittest.TestCase):
    def test_citation_meta(self):
        item = page_to_item(CITATION_PAGE, f"doi:{DOI}", ABS_URL)
        self.assertEqual(item.title, "Meta Carried Title")
        self.assertEqual(item.container_title, "Journal of Fluid Mechanics")
        self.assertEqual(item.page, "55-66")
        self.assertEqual(item.year, "2016")
        self.assertEqual(item.doi, DOI)
        self.assertEqual(item.item_type, "article")
        self.assertEqual(item.url, ABS_URL)

    def test_a_dc_only_page_still_parses(self):
        self.assertEqual(page_to_item(DC_PAGE, "url:x").title, "Dublin Core Title")

    def test_a_title_tag_is_the_last_resort(self):
        item = page_to_item("<html><head><title>Tag Title | Cambridge Core</title></head></html>",
                            "url:x")
        self.assertEqual(item.title, "Tag Title")

    def test_a_chapter_url_is_typed_chapter(self):
        self.assertEqual(page_to_item(CHAPTER_PAGE, "url:x", CHAPTER_URL).item_type, "chapter")

    def test_a_url_key_is_promoted_to_the_doi_from_the_page(self):
        self.assertEqual(page_to_item(CITATION_PAGE, "url:x").doi, DOI)

    def test_the_page_doi_wins_over_the_key(self):
        item = page_to_item(CITATION_PAGE, f"url:{URL}", URL)
        self.assertEqual(item.doi, DOI)


class MarkerTest(unittest.TestCase):
    def test_the_licence_block_is_open_access(self):
        self.assertTrue(is_open_access(OA_PAGE))

    def test_the_wording_alone_is_enough(self):
        self.assertTrue(is_open_access("<p>This is an Open Access article.</p>"))

    def test_a_paywalled_page_is_not(self):
        self.assertFalse(is_open_access(CITATION_PAGE))

    def test_a_bare_cc_link_from_a_related_card_is_not_open_access(self):
        # Related-article cards on a paywalled page link their own CC licences too.
        page = CITATION_PAGE.replace(
            "</head>", '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY</a></head>')
        self.assertFalse(is_open_access(page))

    def test_the_abs_page_is_the_short_page(self):
        self.assertTrue(is_short_page(ABS_URL))
        self.assertFalse(is_short_page(URL))

    def test_a_challenge_page(self):
        self.assertTrue(is_block_page(BLOCK_PAGE, 200))
        self.assertTrue(is_block_page("", 403))
        self.assertFalse(is_block_page(CITATION_PAGE, 200))

    def test_only_cambridge_cookie_rows_are_kept(self):
        raw = "cambridge.org\tTRUE\t/\tTRUE\t0\tsession\tabc\n.example.com\tTRUE\t/\tTRUE\t0\tx\ty"
        self.assertEqual(parse_cookie(raw), {"session": "abc"})


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.be = CambridgeBackend()
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
        rec = stub(self.be, (302, "", {"location": ABS_URL}), (200, CITATION_PAGE))
        self.be.get(DOI)
        self.assertTrue(rec.urls[0].startswith("https://doi.org/"))
        self.assertEqual(rec.urls[-1], ABS_URL)

    def test_the_landed_url_becomes_the_item_url(self):
        stub(self.be, (302, "", {"location": ABS_URL}), (200, CITATION_PAGE))
        self.assertEqual(self.be.get(DOI).url, ABS_URL)

    def test_get_puts_the_pdf_url_in_attachments(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(f"url:{URL}").attachments, [PDF])

    def test_an_open_access_page_marks_the_item(self):
        stub(self.be, (200, OA_PAGE))
        item = self.be.get(f"url:{URL}")
        self.assertTrue(item.extra.get("open_access"))
        self.assertEqual(item.access, "oa")

    def test_a_paywalled_page_is_not_marked_open_access(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertFalse(self.be.get(f"url:{URL}").extra.get("open_access"))

    def test_a_url_key_becomes_a_doi_key_once_the_page_names_it(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(f"url:{URL}").key, f"doi:{DOI}")

    def test_a_page_without_a_pdf_link_has_no_attachments(self):
        stub(self.be, (200, DC_PAGE))
        self.assertEqual(self.be.get(f"url:{URL}").attachments, [])

    def test_404_is_not_found(self):
        stub(self.be, (404, "<html>not found</html>"))
        with self.assertRaises(NotFound):
            self.be.get(DOI)

    def test_a_doi_landing_off_cambridge_is_not_found(self):
        stub(self.be, (302, "", {"location": "https://example.org/x"}), (200, CITATION_PAGE))
        with self.assertRaises(NotFound):
            self.be.get(DOI)

    def test_a_challenge_page_becomes_the_gate_message(self):
        stub(self.be, (403, BLOCK_PAGE))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.get(f"url:{URL}")
        self.assertIn("Cambridge Core", str(ctx.exception))
        self.assertIn("user_agent", str(ctx.exception))

    def test_search_resolves_a_doi(self):
        stub(self.be, (302, "", {"location": URL}), (200, CITATION_PAGE))
        self.assertEqual(len(self.be.search(DOI)), 1)

    def test_search_on_a_word_returns_nothing_and_sends_nothing(self):
        rec = stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.search("cavity formation"), [])
        self.assertEqual(rec.urls, [])

    def test_search_not_found_returns_nothing(self):
        stub(self.be, (404, "x"))
        self.assertEqual(self.be.search(DOI), [])

    def test_get_attachment_returns_the_bytes(self):
        stub(self.be, (200, CITATION_PAGE), (200, b"%PDF-1.7 bytes"))
        self.assertTrue(self.be.get_attachment(f"url:{URL}").startswith(b"%PDF"))

    def test_an_html_answer_on_an_abs_page_names_the_entitlement(self):
        stub(self.be, (200, CITATION_PAGE), (200, "<html>the article page</html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{ABS_URL}")
        self.assertIn("not entitled", str(ctx.exception))

    def test_a_doi_that_redirects_to_the_abs_page_names_the_entitlement_too(self):
        # The real paywalled path: the DOI itself lands on /abs/, which is what is judged.
        stub(self.be, (302, "", {"location": ABS_URL}), (200, CITATION_PAGE),
             (200, "<html>the article page</html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("/abs/ short page", str(ctx.exception))

    def test_an_html_answer_on_a_full_page_says_the_generic_reason(self):
        stub(self.be, (200, CITATION_PAGE), (200, "<html>not a pdf</html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("instead of the PDF", str(ctx.exception))

    def test_an_html_answer_on_an_open_access_page_says_so(self):
        stub(self.be, (200, OA_PAGE), (200, "<html>the article page</html>"))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("open access", str(ctx.exception))

    def test_get_attachment_without_a_link(self):
        stub(self.be, (200, DC_PAGE))
        with self.assertRaises(Unavailable) as ctx:
            self.be.get_attachment(f"url:{URL}")
        self.assertIn("no PDF link", str(ctx.exception))

    def test_a_promoted_url_key_shares_the_memo_with_its_doi(self):
        # fetch() turns the URL into the DOI, so that key must hit the same memo.
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
            CambridgeBackend(cookie="a=1")
        self.assertIn("user_agent", str(ctx.exception))
        self.assertIn("Cambridge Core session cookie", str(ctx.exception))
        # The site has no Cloudflare; the message must not blame one.
        self.assertNotIn("Cloudflare", str(ctx.exception))

    def test_a_cookie_pair_is_sent_with_every_request(self):
        be = CambridgeBackend(cookie="session=abc", user_agent="UA/1")
        speed_up(be)
        rec = stub(be, (200, CITATION_PAGE))
        be.get(f"url:{URL}")
        self.assertIn("session=abc", rec.header("cookie"))
        self.assertEqual(rec.header("user-agent"), "UA/1")

    def test_has_session(self):
        self.assertFalse(CambridgeBackend().has_session)
        self.assertTrue(CambridgeBackend(cookie="a=1", user_agent="UA/1").has_session)


if __name__ == "__main__":
    unittest.main()
