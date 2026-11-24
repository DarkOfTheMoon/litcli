"""Springer backend offline tests: pure parsers and the stubbed fetch path."""
import os
import sys
import unittest
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.springer import (
    SpringerBackend,
    extract_pdf_url,
    is_block_page,
    normalize_key,
    page_to_item,
    parse_cookie,
)
from litcli.errors import BadInput, BotWall, NotFound
from tests.http_stub import speed_up, stub

DOI = "10.1007/s11831-021-09587-6"
URL = f"https://link.springer.com/article/{DOI}"
PDF = f"https://link.springer.com/content/pdf/{DOI}.pdf"

JSONLD = """{
 "@type": "ScholarlyArticle",
 "headline": "A Study of Things",
 "author": [{"@type": "Person", "name": "Jane Doe"}, "John Smith"],
 "datePublished": "2021-05-01",
 "isPartOf": {"name": "Journal of Things", "volumeNumber": "12", "issueNumber": "3"},
 "pageStart": "100", "pageEnd": "110",
 "identifier": "10.1007/jot.2021.01.001",
 "description": "<p>An abstract of things.</p>",
 "publisher": {"name": "Springer"}
}"""

JSONLD_PAGE = ('<html><head><title>ignored title</title>'
               f'<script type="application/ld+json">{JSONLD}</script>'
               "</head><body></body></html>")

CITATION_PAGE = f"""<html><head>
<meta name="citation_title" content="Meta Carried Title">
<meta name="citation_author" content="Doe, Jane">
<meta name="citation_author" content="Smith, John">
<meta name="citation_journal_title" content="Meta Journal">
<meta name="citation_volume" content="7">
<meta name="citation_issue" content="2">
<meta name="citation_firstpage" content="55">
<meta name="citation_lastpage" content="66">
<meta name="citation_publication_date" content="2019/03/04">
<meta name="citation_doi" content="{DOI}">
<meta name="citation_abstract" content="A meta abstract.">
<meta name="citation_pdf_url" content="{PDF}">
</head><body></body></html>"""

DC_PAGE = """<html><head>
<meta name="dc.title" content="Dublin Core Title">
<meta name="dc.creator" content="Roe, Richard">
<meta name="dc.source" content="DC Journal">
<meta name="dc.date" content="2018">
<meta name="dc.identifier" content="10.1007/j.dc.2018.01.003">
</head><body></body></html>"""

BLOCK_PAGE = """<html><head><title>Just a moment...</title></head><body>
<div><p>Enable JavaScript and cookies to continue</p></div></body></html>"""


class NormalizeKeyTest(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(normalize_key(DOI), f"doi:{DOI}")

    def test_springer_prefix(self):
        self.assertEqual(normalize_key(f"springer:{DOI}"), f"doi:{DOI}")

    def test_source_prefix_form_is_accepted(self):
        # The table prints ``source/key``; a pasted key must resolve like ``springer:<DOI>``.
        self.assertEqual(normalize_key(f"springer/{DOI}"), f"doi:{DOI}")

    def test_doi_url(self):
        self.assertEqual(normalize_key(f"https://doi.org/{DOI}"), f"doi:{DOI}")

    def test_article_url(self):
        self.assertEqual(normalize_key(URL), f"doi:{DOI}")

    def test_chapter_and_content_urls(self):
        self.assertEqual(normalize_key(f"https://link.springer.com/chapter/{DOI}"),
                         f"doi:{DOI}")
        self.assertEqual(normalize_key(PDF), f"doi:{DOI}")

    def test_url_with_query_is_clean(self):
        # The site itself writes ``?error=cookies_not_supported&code=…`` onto landings.
        self.assertEqual(normalize_key(f"{URL}?error=cookies_not_supported&code=abc"),
                         f"doi:{DOI}")

    def test_percent_encoded_url_doi_is_decoded(self):
        self.assertEqual(
            normalize_key("https://link.springer.com/article/10.1007%2Fs11831-021-09587-6"),
            f"doi:{DOI}")

    def test_a_source_prefixed_doi_key_is_accepted(self):
        # ``render.py`` prints ``source/key`` and the key itself already says ``doi:``.
        self.assertEqual(normalize_key(f"springer/doi:{DOI}"), f"doi:{DOI}")

    def test_a_nature_url_becomes_its_doi(self):
        self.assertEqual(normalize_key("https://www.nature.com/articles/s41598-024-80509-9"),
                         "doi:10.1038/s41598-024-80509-9")

    def test_a_nature_pdf_url_becomes_its_doi(self):
        self.assertEqual(normalize_key("https://www.nature.com/articles/s41598-024-80509-9.pdf"),
                         "doi:10.1038/s41598-024-80509-9")

    def test_a_nature_epdf_url_becomes_its_doi(self):
        # The ``.epdf`` suffix is a viewer form, like ``.pdf``: it is not part of the slug.
        self.assertEqual(
            normalize_key("https://www.nature.com/articles/s41598-024-80509-9.epdf"),
            "doi:10.1038/s41598-024-80509-9")

    def test_a_doi_query_tail_is_not_part_of_the_doi(self):
        self.assertEqual(normalize_key(f"{DOI}?download=true"), f"doi:{DOI}")

    def test_a_bmc_url_becomes_its_doi(self):
        self.assertEqual(
            normalize_key("https://bmcmedicine.biomedcentral.com/articles/10.1186/s12916-021-01941-6"),
            "doi:10.1186/s12916-021-01941-6")

    def test_a_sub_page_is_not_part_of_the_doi(self):
        # /article/<DOI>/figures/1: the tail must not end up in the key or the saved filename.
        self.assertEqual(normalize_key(f"{URL}/figures/1"), f"doi:{DOI}")
        self.assertEqual(normalize_key(f"{URL}/sections/1"), f"doi:{DOI}")

    def test_a_real_doi_slash_survives_trimming(self):
        chapter = "10.1007/978-3-030-05861-6_3"
        self.assertEqual(normalize_key(f"https://link.springer.com/chapter/{chapter}"),
                         f"doi:{chapter}")

    def test_keyword_is_bad_input(self):
        with self.assertRaises(BadInput):
            normalize_key("finite volume method")

    def test_empty_is_bad_input(self):
        with self.assertRaises(BadInput):
            normalize_key("  ")


class CookieTest(unittest.TestCase):
    def test_netscape_rows_of_other_hosts_are_dropped(self):
        raw = ("# Netscape HTTP Cookie File\n"
               ".springer.com\tTRUE\t/\tTRUE\t0\tsim-inst-token\tabc\n"
               ".example.com\tTRUE\t/\tTRUE\t0\tother\tzzz\n")
        self.assertEqual(parse_cookie(raw), {"sim-inst-token": "abc"})

    def test_header_string_is_parsed(self):
        self.assertEqual(parse_cookie("Cookie: a=1; b=2"), {"a": "1", "b": "2"})

    def test_nature_rows_are_kept_for_nature_branded_articles(self):
        # 10.1038 articles keep their session on nature.com; the export must not drop it.
        raw = ".nature.com\tTRUE\t/\tTRUE\t0\tidp_session\tn1\n"
        self.assertEqual(parse_cookie(raw), {"idp_session": "n1"})


class BlockPageTest(unittest.TestCase):
    def test_marker_detected(self):
        self.assertTrue(is_block_page(BLOCK_PAGE))

    def test_plain_403_detected(self):
        self.assertTrue(is_block_page("", status=403))

    def test_normal_page_not_blocked(self):
        self.assertFalse(is_block_page(CITATION_PAGE))

    def test_served_page_carrying_the_challenge_platform_script_is_not_a_block(self):
        page = ('<html><head><title>A Title | SpringerLink</title></head><body>'
                '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>'
                '</body></html>')
        self.assertFalse(is_block_page(page))


class PageToItemTest(unittest.TestCase):
    def test_citation_meta_is_preferred_over_json_ld(self):
        page = citation_page_with(JSONLD_PAGE)
        it = page_to_item(page, f"doi:{DOI}")
        self.assertEqual(it.source, "springer")
        self.assertEqual(it.key, f"doi:{DOI}")
        self.assertEqual(it.title, "Meta Carried Title")
        self.assertEqual(it.creators, [{"family": "Doe", "given": "Jane"},
                                       {"family": "Smith", "given": "John"}])
        self.assertEqual(it.year, "2019")
        self.assertEqual(it.container_title, "Meta Journal")
        self.assertEqual(it.volume, "7")
        self.assertEqual(it.issue, "2")
        self.assertEqual(it.page, "55-66")
        self.assertEqual(it.doi, DOI)
        self.assertEqual(it.abstract, "A meta abstract.")
        self.assertEqual(it.url, URL)
        self.assertEqual(it.item_type, "article")

    def test_json_ld_fallback_when_no_citation_meta(self):
        it = page_to_item(JSONLD_PAGE, f"doi:{DOI}")
        self.assertEqual(it.title, "A Study of Things")
        self.assertEqual(it.creators, [{"family": "Doe", "given": "Jane"},
                                       {"family": "Smith", "given": "John"}])
        self.assertEqual(it.container_title, "Journal of Things")
        self.assertEqual(it.page, "100-110")
        self.assertEqual(it.publisher, "Springer")
        # A related card's record naming another DOI does not rename the article.
        self.assertEqual(it.doi, DOI)

    def test_dc_fallback(self):
        it = page_to_item(DC_PAGE, f"doi:{DOI}")
        self.assertEqual(it.title, "Dublin Core Title")
        self.assertEqual(it.creators, [{"family": "Roe", "given": "Richard"}])
        self.assertEqual(it.year, "2018")
        self.assertEqual(it.doi, "10.1007/j.dc.2018.01.003")

    def test_title_tag_is_last_resort_and_drops_the_site_suffix(self):
        page = "<html><head><title>Only a Title | SpringerLink</title></head></html>"
        self.assertEqual(page_to_item(page, f"doi:{DOI}").title, "Only a Title")

    def test_json_ld_fills_the_authors_the_meta_tags_lack(self):
        # A title-only meta page must still get its authors from the JSON-LD record.
        page = ('<html><head><meta name="citation_title" content="Meta Title">'
                f'<script type="application/ld+json">{JSONLD}</script></head></html>')
        it = page_to_item(page, f"doi:{DOI}")
        self.assertEqual(it.title, "Meta Title")
        self.assertEqual([c["family"] for c in it.creators], ["Doe", "Smith"])


def citation_page_with(extra: str) -> str:
    return CITATION_PAGE.replace("</head>", extra).replace("</body>", "</body>")


class ExtractPdfUrlTest(unittest.TestCase):
    def test_citation_pdf_meta_is_used(self):
        page = f'<meta name="citation_pdf_url" content="{PDF}">'
        self.assertEqual(extract_pdf_url(page, DOI), PDF)

    def test_content_anchor_is_used_when_no_meta(self):
        page = f'<a href="/content/pdf/{DOI}.pdf">Download PDF</a>'
        self.assertEqual(extract_pdf_url(page, DOI), PDF)

    def test_meta_of_another_doi_is_ignored(self):
        page = ('<meta name="citation_pdf_url" '
                'content="https://link.springer.com/content/pdf/10.1007/zzz.1.pdf">')
        self.assertEqual(extract_pdf_url(page, DOI), "")

    def test_unrelated_cards_are_ignored(self):
        page = '<a href="/content/pdf/10.1007/other.2000.001.pdf">PDF</a>'
        self.assertEqual(extract_pdf_url(page, DOI), "")

    def test_nature_host_is_allowed(self):
        # Nature-branded content is served from nature.com; the meta points there.
        page = '<meta name="citation_pdf_url" content="https://www.nature.com/articles/s41419-020-2298-2.pdf">'
        self.assertEqual(extract_pdf_url(page, "10.1038/s41419-020-2298-2"),
                         "https://www.nature.com/articles/s41419-020-2298-2.pdf")

    def test_a_nature_meta_of_another_doi_is_ignored(self):
        page = ('<meta name="citation_pdf_url" '
                'content="https://www.nature.com/articles/s41419-020-2298-2.pdf">')
        self.assertEqual(extract_pdf_url(page, "10.1038/other.1"), "")

    def test_missing_returns_empty(self):
        self.assertEqual(extract_pdf_url(JSONLD_PAGE, DOI), "")

    def test_bad_host_raises(self):
        page = '<meta name="citation_pdf_url" content="https://evil.example.com/x.pdf">'
        with self.assertRaises(RuntimeError):
            extract_pdf_url(page, DOI)


class SpringerBackendTest(unittest.TestCase):
    def setUp(self):
        self.be = SpringerBackend()
        speed_up(self.be)

    def test_capabilities_are_read_only(self):
        self.assertEqual(self.be.capabilities, {"read"})

    def test_access_is_institutional_not_oa(self):
        self.assertEqual(self.be.access, "entitled")

    def test_get_puts_the_pdf_url_in_attachments(self):
        stub(self.be, (200, CITATION_PAGE))
        it = self.be.get(DOI)
        self.assertEqual(it.attachments, [PDF])

    def test_an_open_access_page_is_marked_on_the_item(self):
        page = CITATION_PAGE.replace("</head>", '<span data-test="open-access"></span></head>')
        stub(self.be, (200, page))
        self.assertTrue(self.be.get(DOI).extra.get("open_access"))

    def test_a_paywalled_page_is_not_marked_open_access(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertFalse(self.be.get(DOI).extra.get("open_access"))

    def test_get_without_a_pdf_link_has_no_attachments(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.get(DOI).attachments, [])

    def test_get_by_doi_fetches_the_article_url(self):
        rec = stub(self.be, (200, CITATION_PAGE))
        self.be.get(DOI)
        self.assertEqual(rec.urls[0], URL)

    def test_get_accepts_an_article_url(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(f"{URL}?error=cookies_not_supported&code=x").title,
                         "Meta Carried Title")

    def test_404_is_not_found(self):
        rec = stub(self.be, (404, "<html>not found</html>"))
        with self.assertRaises(NotFound):
            self.be.get(DOI)
        # Every page kind was tried before giving up.
        self.assertEqual([urlparse(u).path.split("/")[1] for u in rec.urls],
                         ["article", "chapter", "referenceworkentry"])

    def test_a_chapter_doi_falls_back_to_the_chapter_path(self):
        # A book chapter lives only under /chapter/; /article/ 404s.
        rec = stub(self.be, (404, "<html>not found</html>"), (200, CITATION_PAGE))
        it = self.be.get(DOI)
        self.assertEqual(rec.urls[-1], f"https://link.springer.com/chapter/{DOI}")
        self.assertEqual(it.url, rec.urls[-1])
        self.assertEqual(it.item_type, "chapter")

    def test_a_reference_work_entry_path_is_also_tried(self):
        stub(self.be, (404, "x"), (404, "x"), (200, CITATION_PAGE))
        self.assertEqual(self.be.get(DOI).url,
                         f"https://link.springer.com/referenceworkentry/{DOI}")

    def test_a_chapter_page_is_fetched_once_for_the_download(self):
        rec = stub(self.be, (404, "x"), (200, CITATION_PAGE), (200, b"%PDF-1.7 bytes"))
        self.be.get_attachment(DOI)
        self.assertEqual(len(rec.requests), 3)  # /article/ 404, the chapter page, the PDF

    def test_404_search_returns_empty(self):
        stub(self.be, (404, "<html>not found</html>"))
        self.assertEqual(self.be.search(DOI), [])

    def test_block_page_raises_a_named_error(self):
        stub(self.be, (403, BLOCK_PAGE))
        with self.assertRaises(BotWall) as ctx:
            self.be.get(DOI)
        self.assertIn("cookie", str(ctx.exception))

    def test_5xx_retries_then_succeeds(self):
        rec = stub(self.be, (503, "busy"), (200, CITATION_PAGE))
        self.assertEqual(self.be.get(DOI).title, "Meta Carried Title")
        self.assertEqual(len(rec.requests), 2)

    def test_a_sign_in_page_is_not_accepted_as_the_item(self):
        # This ``get`` is its own override (three path kinds), so it must call the guard itself.
        stub(self.be, (200, "<html><head><title>Log in to SpringerLink</title></head>"
                            '<body><a href="/login">Log in</a></body></html>'))
        with self.assertRaises(BotWall) as ctx:
            self.be.get(DOI)
        self.assertIn("sign-in page", str(ctx.exception))

    def test_unparseable_page_raises(self):
        stub(self.be, (200, "<html><body></body></html>"))
        with self.assertRaises(RuntimeError):
            self.be.get(DOI)

    def test_search_keyword_returns_empty_without_a_request(self):
        rec = stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.search("finite volume method"), [])
        self.assertEqual(rec.urls, [])

    def test_search_doi_returns_the_single_item(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual([i.key for i in self.be.search(DOI)], [f"doi:{DOI}"])

    def test_search_zero_limit_is_empty(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.search(DOI, limit=0), [])

    def test_get_attachment_downloads_the_pdf(self):
        rec = stub(self.be, (200, CITATION_PAGE), (200, b"%PDF-1.7 bytes"))
        data = self.be.get_attachment(DOI)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(rec.urls[1], PDF)
        self.assertEqual(rec.requests[1].headers.get("accept"), "application/pdf")

    def test_get_attachment_without_a_pdf_link_raises(self):
        stub(self.be, (200, JSONLD_PAGE))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("no PDF link", str(ctx.exception))

    def test_anonymous_session_landing_on_the_page_is_named(self):
        # Unentitled and not open access: the endpoint sends the client back to the page.
        stub(self.be, (200, CITATION_PAGE), (200, "<html>article page, no bytes</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("no institution token", str(ctx.exception))
        self.assertIn("cookie+user_agent", str(ctx.exception))

    def test_a_quoted_empty_institution_token_counts_as_anonymous(self):
        # The site sets ``sim-inst-token=""`` (quoted) for anonymous visitors; httpx keeps the
        # quotes, so the empty token must not read as an entitlement.
        token = {"set-cookie": 'sim-inst-token=""; Domain=.springer.com; Path=/'}
        stub(self.be, (200, CITATION_PAGE, token), (200, "<html>article page</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("no institution token", str(ctx.exception))

    def test_entitled_session_that_is_refused_is_named_differently(self):
        token = {"set-cookie": "sim-inst-token=abc123; Domain=.springer.com; Path=/"}
        stub(self.be, (200, CITATION_PAGE, token), (200, "<html>article page, no bytes</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("institution token was not accepted", str(ctx.exception))

    def test_a_configured_cookie_counts_as_entitled_without_reaching_the_jar(self):
        # The configured cookie travels as a header and never enters the jar, so the site's
        # anonymous `set-cookie: sim-inst-token=""` must not read as "nothing was configured".
        be = SpringerBackend(cookie="sim-inst-token=abc123", user_agent="UA/1")
        speed_up(be)
        token = {"set-cookie": 'sim-inst-token=""; Domain=.springer.com; Path=/'}
        stub(be, (200, CITATION_PAGE, token), (200, "<html>article page, no bytes</html>"))
        with self.assertRaises(Exception) as ctx:
            be.get_attachment(DOI)
        self.assertIn("institution token was not accepted", str(ctx.exception))
        self.assertNotIn("holds no institution token", str(ctx.exception))

    def test_an_open_access_item_carries_the_oa_access(self):
        from litcli import provenance

        page = CITATION_PAGE.replace("</head>", '<span data-test="open-access"></span></head>')
        stub(self.be, (200, page))
        self.assertEqual(self.be.get(DOI).access, provenance.OA)

    def test_a_paywalled_item_leaves_the_access_to_the_stamp(self):
        stub(self.be, (200, CITATION_PAGE))
        self.assertEqual(self.be.get(DOI).access, "")

    def test_an_open_access_page_that_still_answers_html_is_named_as_such(self):
        page = CITATION_PAGE.replace("</head>", '<span data-test="open-access"></span></head>')
        stub(self.be, (200, page), (200, "<html>article page</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("open access", str(ctx.exception))

    def test_get_attachment_403_is_reported_as_gated(self):
        stub(self.be, (200, CITATION_PAGE), (403, BLOCK_PAGE))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("gated", str(ctx.exception))

    def test_get_then_attachment_reuses_the_page(self):
        rec = stub(self.be, (200, CITATION_PAGE), (200, b"%PDF-1.7 bytes"))
        self.be.get(DOI)
        self.be.get_attachment(DOI)
        self.assertEqual(len(rec.urls), 2)  # the page and the PDF, not the page twice


class ConstructionTest(unittest.TestCase):
    def test_a_cookie_without_a_user_agent_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            SpringerBackend(cookie="a=1")
        self.assertIn("user_agent", str(ctx.exception))
        # The site has no Cloudflare; the message must not blame one.
        self.assertIn("SpringerLink identity cookie", str(ctx.exception))
        self.assertNotIn("Cloudflare", str(ctx.exception))

    def test_a_cookie_pair_is_sent_with_every_request(self):
        be = SpringerBackend(cookie="sim-inst-token=abc", user_agent="UA/1")
        speed_up(be)
        rec = stub(be, (200, CITATION_PAGE))
        be.get(DOI)
        self.assertIn("sim-inst-token=abc", rec.requests[0].headers.get("cookie", ""))


if __name__ == "__main__":
    unittest.main()
