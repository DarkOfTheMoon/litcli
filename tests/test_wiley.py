"""Wiley backend offline tests: pure parsers and the stubbed fetch path."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.wiley import (
    WileyBackend,
    extract_pdf_url,
    is_block_page,
    normalize_key,
    page_to_item,
    parse_cookie,
)
from litcli.errors import BadInput
from tests.http_stub import speed_up, stub

DOI = "10.1002/nme.1628"
URL = f"https://onlinelibrary.wiley.com/doi/{DOI}"
SHELL = f"https://onlinelibrary.wiley.com/doi/pdf/{DOI}"  # view: an HTML page, not the bytes
PDF = f"https://onlinelibrary.wiley.com/doi/pdfdirect/{DOI}"  # what extract_pdf_url normalizes to

JSONLD_PAGE = """<html><head><title>ignored title</title>
<script type="application/ld+json">{
 "@type": "ScholarlyArticle",
 "headline": "A Study of Things",
 "author": [{"@type": "Person", "name": "Jane Doe"}, "John Smith"],
 "datePublished": "2021-05-01",
 "isPartOf": {"name": "Journal of Things", "volumeNumber": "12", "issueNumber": "3"},
 "pageStart": "100", "pageEnd": "110",
 "identifier": "10.1002/jot.2021.01.001",
 "description": "<p>An abstract of things.</p>",
 "publisher": {"name": "Wiley"}
}</script></head><body></body></html>"""

CITATION_PAGE = """<html><head>
<meta name="citation_title" content="Meta Carried Title">
<meta name="citation_author" content="Doe, Jane">
<meta name="citation_author" content="Smith, John">
<meta name="citation_journal_title" content="Meta Journal">
<meta name="citation_volume" content="7">
<meta name="citation_issue" content="2">
<meta name="citation_firstpage" content="55">
<meta name="citation_lastpage" content="66">
<meta name="citation_publication_date" content="2019/03/04">
<meta name="citation_doi" content="10.1002/j.meta.2019.01.002">
<meta name="citation_abstract" content="A meta abstract.">
</head><body></body></html>"""

DC_PAGE = """<html><head>
<meta name="dc.title" content="Dublin Core Title">
<meta name="dc.creator" content="Roe, Richard">
<meta name="dc.source" content="DC Journal">
<meta name="dc.date" content="2018">
<meta name="dc.identifier" content="10.1002/j.dc.2018.01.003">
</head><body></body></html>"""

BLOCK_PAGE = """<html><head><title>Just a moment...</title></head><body>
<div id="challenge-platform"><p>Error CPE00001 — are you a robot?</p></div></body></html>"""


def _pdf_anchor(doi, mode="pdf"):
    return f'<a class="pdf-download" href="/doi/{mode}/{doi}">PDF</a>'


class NormalizeKeyTest(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(normalize_key(DOI), f"doi:{DOI}")

    def test_wiley_prefix(self):
        self.assertEqual(normalize_key(f"wiley:{DOI}"), f"doi:{DOI}")

    def test_doi_url(self):
        self.assertEqual(normalize_key(f"https://doi.org/{DOI}"), f"doi:{DOI}")

    def test_article_url(self):
        self.assertEqual(normalize_key(URL), f"doi:{DOI}")

    def test_abs_full_epdf_pdf_urls(self):
        for mode in ("abs", "full", "epdf", "pdf", "pdfdirect"):
            self.assertEqual(normalize_key(f"https://onlinelibrary.wiley.com/doi/{mode}/{DOI}"),
                             f"doi:{DOI}")

    def test_percent_encoded_url_doi_is_decoded(self):
        # ``10.1002%2Fa%2Fb`` is how the TDM API writes the DOI; a pasted URL may carry it.
        self.assertEqual(normalize_key("https://onlinelibrary.wiley.com/doi/pdf/10.1002%2Fa%2Fb"),
                         "doi:10.1002/a/b")

    def test_keyword_is_bad_input(self):
        with self.assertRaises(BadInput):
            normalize_key("finite volume method")

    def test_empty_is_bad_input(self):
        with self.assertRaises(BadInput):
            normalize_key("  ")


class BlockPageTest(unittest.TestCase):
    def test_marker_detected(self):
        self.assertTrue(is_block_page(BLOCK_PAGE))

    def test_plain_403_detected(self):
        self.assertTrue(is_block_page("", status=403))

    def test_normal_page_not_blocked(self):
        self.assertFalse(is_block_page(JSONLD_PAGE))

    def test_served_page_carrying_the_challenge_platform_script_is_not_a_block(self):
        # Cloudflare injects /cdn-cgi/challenge-platform/... into every served page too.
        page = ('<html><head><title>A Title - Wiley Online Library</title></head><body>'
                '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>'
                '</body></html>')
        self.assertFalse(is_block_page(page))

    def test_challenge_title_alone_is_detected(self):
        self.assertTrue(is_block_page('<html><title>Just a moment...</title></html>'))


class PageToItemTest(unittest.TestCase):
    def test_json_ld_is_preferred(self):
        it = page_to_item(JSONLD_PAGE, f"doi:{DOI}")
        self.assertEqual(it.source, "wiley")
        self.assertEqual(it.key, f"doi:{DOI}")
        self.assertEqual(it.title, "A Study of Things")
        self.assertEqual(it.creators, [{"family": "Doe", "given": "Jane"},
                                       {"family": "Smith", "given": "John"}])
        self.assertEqual(it.year, "2021")
        self.assertEqual(it.container_title, "Journal of Things")
        self.assertEqual(it.volume, "12")
        self.assertEqual(it.issue, "3")
        self.assertEqual(it.page, "100-110")
        # The plain record names another DOI: the requested one is this article's identity.
        self.assertEqual(it.doi, DOI)
        self.assertEqual(it.abstract, "An abstract of things.")
        self.assertEqual(it.publisher, "Wiley")
        self.assertEqual(it.url, f"https://onlinelibrary.wiley.com/doi/{DOI}")
        self.assertEqual(it.item_type, "article")

    def test_json_ld_of_the_requested_doi_beats_an_earlier_related_one(self):
        # Related/recommended cards can ship their own record ahead of the article's own.
        page = f"""<html><head>
<script type="application/ld+json">{{"@type": "Article", "headline": "RELATED",
 "identifier": "10.1002/other.1"}}</script>
<script type="application/ld+json">{{"@type": "ScholarlyArticle", "headline": "THE ARTICLE",
 "identifier": "{DOI}"}}</script></head><body></body></html>"""
        it = page_to_item(page, f"doi:{DOI}")
        self.assertEqual(it.title, "THE ARTICLE")
        self.assertEqual(it.doi, DOI)

    def test_citation_meta_variant(self):
        it = page_to_item(CITATION_PAGE, f"doi:{DOI}")
        self.assertEqual(it.title, "Meta Carried Title")
        self.assertEqual(it.creators, [{"family": "Doe", "given": "Jane"},
                                       {"family": "Smith", "given": "John"}])
        self.assertEqual(it.container_title, "Meta Journal")
        self.assertEqual(it.year, "2019")
        self.assertEqual(it.page, "55-66")
        self.assertEqual(it.doi, "10.1002/j.meta.2019.01.002")
        self.assertEqual(it.abstract, "A meta abstract.")

    def test_dc_fallback(self):
        it = page_to_item(DC_PAGE, f"doi:{DOI}")
        self.assertEqual(it.title, "Dublin Core Title")
        self.assertEqual(it.creators, [{"family": "Roe", "given": "Richard"}])
        self.assertEqual(it.container_title, "DC Journal")
        self.assertEqual(it.year, "2018")
        self.assertEqual(it.doi, "10.1002/j.dc.2018.01.003")

    def test_title_tag_is_last_resort_and_drops_the_site_suffix(self):
        page = "<html><head><title>Only a Title - Wiley Online Library</title></head></html>"
        self.assertEqual(page_to_item(page, f"doi:{DOI}").title, "Only a Title")


class ExtractPdfUrlTest(unittest.TestCase):
    def test_citation_pdf_meta_is_rewritten_to_pdfdirect(self):
        # The meta is always present (even unentitled) and names the HTML shell.
        page = f'<meta name="citation_pdf_url" content="{SHELL}">'
        self.assertEqual(extract_pdf_url(page, DOI), PDF)

    def test_pdf_href_of_the_article_is_rewritten_to_pdfdirect(self):
        page = _pdf_anchor(DOI)
        self.assertEqual(extract_pdf_url(page, DOI), PDF)

    def test_pdfdirect_anchor_beats_the_meta(self):
        page = (f'<meta name="citation_pdf_url" content="{SHELL}">'
                f'<a href="/doi/pdfdirect/{DOI}?download=true">PDF</a>')
        self.assertEqual(extract_pdf_url(page, DOI), f"{PDF}?download=true")

    def test_meta_of_another_doi_is_ignored(self):
        page = '<meta name="citation_pdf_url" content="https://onlinelibrary.wiley.com/doi/pdf/10.1002/zzz.1">'
        self.assertEqual(extract_pdf_url(page, DOI), "")

    def test_pdfdirect_href_is_taken(self):
        page = f'<a href="/doi/pdfdirect/{DOI}?download=true">PDF</a>'
        self.assertEqual(extract_pdf_url(page, DOI),
                         f"https://onlinelibrary.wiley.com/doi/pdfdirect/{DOI}?download=true")

    def test_unrelated_cards_are_ignored(self):
        page = _pdf_anchor("10.1002/other.2000.001")
        self.assertEqual(extract_pdf_url(page, DOI), "")

    def test_missing_returns_empty(self):
        self.assertEqual(extract_pdf_url(JSONLD_PAGE, DOI), "")

    def test_bad_host_raises(self):
        page = '<meta name="citation_pdf_url" content="https://evil.example.com/x.pdf">'
        with self.assertRaises(RuntimeError):
            extract_pdf_url(page, DOI)


class WileyBackendTest(unittest.TestCase):
    def setUp(self):
        self.be = WileyBackend()
        speed_up(self.be)

    def test_capabilities_are_read_only(self):
        self.assertEqual(self.be.capabilities, {"read"})

    def test_access_is_institutional_not_oa(self):
        self.assertEqual(self.be.access, "entitled")

    def test_get_puts_the_pdf_url_in_attachments(self):
        stub(self.be, (200, CITATION_PAGE + _pdf_anchor(DOI)))
        it = self.be.get(DOI)
        self.assertEqual(it.attachments, [PDF])

    def test_get_without_a_pdf_link_has_no_attachments(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.get(DOI).attachments, [])

    def test_get_by_doi_fetches_the_article_url(self):
        rec = stub(self.be, (200, JSONLD_PAGE))
        self.be.get(DOI)
        self.assertEqual(rec.urls[0], f"https://onlinelibrary.wiley.com/doi/{DOI}")

    def test_get_accepts_an_article_url(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.get(URL).title, "A Study of Things")

    def test_403_block_page_raises_a_named_error(self):
        stub(self.be, (403, BLOCK_PAGE))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.get(DOI)
        self.assertIn("session", str(ctx.exception))
        self.assertIn("user_agent", str(ctx.exception))

    def test_200_block_marker_raises(self):
        stub(self.be, (200, BLOCK_PAGE))
        with self.assertRaises(RuntimeError):
            self.be.get(DOI)

    def test_cloudflare_mitigated_header_raises(self):
        stub(self.be, (200, JSONLD_PAGE, {"cf-mitigated": "challenge"}))
        with self.assertRaises(RuntimeError):
            self.be.get(DOI)

    def test_5xx_retries_then_succeeds(self):
        rec = stub(self.be, (503, "busy"), (200, JSONLD_PAGE))
        self.assertEqual(self.be.get(DOI).title, "A Study of Things")
        self.assertEqual(len(rec.requests), 2)

    def test_unparseable_page_raises(self):
        stub(self.be, (200, "<html><body></body></html>"))
        with self.assertRaises(RuntimeError):
            self.be.get(DOI)

    def test_search_keyword_returns_empty_without_a_request(self):
        rec = stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.search("finite volume method"), [])
        self.assertEqual(rec.urls, [])

    def test_search_doi_returns_the_single_item(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual([i.key for i in self.be.search(DOI)], [f"doi:{DOI}"])

    def test_search_zero_limit_is_empty(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.search(DOI, limit=0), [])

    def test_get_attachment_downloads_the_pdf(self):
        rec = stub(self.be, (200, CITATION_PAGE + _pdf_anchor(DOI)), (200, b"%PDF-1.7 bytes"))
        data = self.be.get_attachment(DOI)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn("/doi/pdfdirect/", rec.urls[1])

    def test_get_attachment_without_a_pdf_link_raises(self):
        stub(self.be, (200, JSONLD_PAGE))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("no PDF link", str(ctx.exception))

    def test_get_attachment_non_pdf_body_is_reported_as_not_entitled(self):
        # An unentitled session gets the PDF URL redirected to the abstract page (HTML).
        stub(self.be, (200, CITATION_PAGE + _pdf_anchor(DOI)), (200, "<html>login</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("not entitled", str(ctx.exception))
        self.assertIn("cookie+user_agent", str(ctx.exception))

    def test_access_denied_redirect_is_named(self):
        # Unentitled: pdfdirect 302s to /doi/abs/ with atypon-access-denied: true.
        stub(self.be, (200, CITATION_PAGE + _pdf_anchor(DOI)),
             (302, "", {"location": f"https://onlinelibrary.wiley.com/doi/abs/{DOI}",
                        "atypon-access-denied": "true"}),
             (200, "<html>abstract only</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("atypon-access-denied", str(ctx.exception))
        self.assertIn(f"/doi/abs/{DOI}", str(ctx.exception))

    def test_get_attachment_403_is_reported_as_gated(self):
        stub(self.be, (200, CITATION_PAGE + _pdf_anchor(DOI)), (403, BLOCK_PAGE))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("gated", str(ctx.exception))

    def test_get_then_attachment_reuses_the_page(self):
        rec = stub(self.be, (200, CITATION_PAGE + _pdf_anchor(DOI)), (200, b"%PDF-1.7 bytes"))
        self.be.get(DOI)
        self.be.get_attachment(DOI)
        self.assertEqual(len(rec.urls), 2)  # the page and the PDF, not the page twice

    def test_tdm_url_encodes_a_decoded_doi_once(self):
        self.be.tdm_token = "a-token"
        rec = stub(self.be, (200, b"%PDF-1.7 bytes"))
        self.be.get_attachment("https://onlinelibrary.wiley.com/doi/pdf/10.1002%2Fa%2Fb")
        self.assertTrue(rec.urls[0].endswith("10.1002%2Fa%2Fb"), rec.urls[0])

    def test_tdm_token_downloads_without_touching_the_site(self):
        self.be.tdm_token = "a-token"
        rec = stub(self.be, (200, b"%PDF-1.7 via tdm"))
        self.assertTrue(self.be.get_attachment(DOI).startswith(b"%PDF"))
        self.assertEqual(len(rec.urls), 1)
        self.assertTrue(rec.urls[0].startswith("https://api.wiley.com/onlinelibrary/tdm/v1/articles/"))
        self.assertIn(DOI.replace("/", "%2F"), rec.urls[0])
        self.assertEqual(rec.requests[0].headers.get("Wiley-TDM-Client-Token"), "a-token")

    def test_gated_page_with_a_token_still_yields_the_doi(self):
        # The token route needs no page; losing metadata must not lose the article.
        self.be.tdm_token = "a-token"
        stub(self.be, (403, BLOCK_PAGE))
        it = self.be.get(DOI)
        self.assertEqual(it.doi, DOI)
        self.assertEqual(it.key, f"doi:{DOI}")
        tdm = f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{DOI.replace('/', '%2F')}"
        self.assertEqual(it.attachments, [tdm])
        # The TDM endpoint answers 400 in a browser, so the openable URL stays the page.
        self.assertEqual(it.extra["tdm_url"], tdm)
        self.assertEqual(it.url, URL)

    def test_gated_page_without_a_token_still_raises(self):
        stub(self.be, (403, BLOCK_PAGE))
        with self.assertRaises(RuntimeError) as ctx:
            self.be.get(DOI)
        self.assertIn("user_agent", str(ctx.exception))

    def test_tdm_403_is_not_entitled_not_a_gate(self):
        # No usable page either: the 403 then is the answer that gets reported.
        self.be.tdm_token = "a-token"
        stub(self.be, (403, "forbidden"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(DOI)
        self.assertIn("not entitled", str(ctx.exception))
        self.assertIn("subscribes", str(ctx.exception))

    def test_tdm_403_falls_back_to_a_working_page_route(self):
        # A stale or mistyped token must not cost a cookie session that can download.
        self.be.tdm_token = "stale"
        rec = stub(self.be, (403, "forbidden"), (200, CITATION_PAGE + _pdf_anchor(DOI)),
                   (200, b"%PDF-1.7 from the page"))
        self.assertTrue(self.be.get_attachment(DOI).endswith(b"from the page"))
        self.assertEqual(rec.requests[0].headers.get("wiley-tdm-client-token"), "stale")
        self.assertIn("/doi/pdfdirect/", rec.urls[2])

    def test_tdm_400_falls_back_to_the_page_route(self):
        # 400 is the missing/invalid-token answer on the first hop.
        self.be.tdm_token = "a-token"
        rec = stub(self.be, (400, ""), (200, CITATION_PAGE + _pdf_anchor(DOI)),
                   (200, b"%PDF-1.7 from the page"))
        self.be.get_attachment(DOI)
        self.assertEqual(len(rec.urls), 3)

    def test_tdm_404_falls_back_to_the_page_route(self):
        self.be.tdm_token = "a-token"
        rec = stub(self.be, (404, "no such article"), (200, CITATION_PAGE + _pdf_anchor(DOI)),
                   (200, b"%PDF-1.7 from the page"))
        self.assertTrue(self.be.get_attachment(DOI).endswith(b"from the page"))
        self.assertEqual(len(rec.urls), 3)


class StatusTest(unittest.TestCase):
    def _status(self, be):
        import contextlib
        import io
        from unittest import mock

        from litcli.commands import diagnostics

        buf = io.StringIO()
        with mock.patch.object(diagnostics, "get_all_backends", return_value={"wiley": be}), \
             contextlib.redirect_stdout(buf):
            diagnostics.cmd_backends_status({}, None)
        return buf.getvalue()

    def test_status_names_cookie_and_tdm_token(self):
        be = WileyBackend(cookie="a=b", user_agent="Mozilla/5.0 Mine", tdm_token="t")
        out = self._status(be)
        self.assertIn("session cookie configured", out)
        self.assertIn("TDM token configured", out)
        self.assertNotIn("TDM token configured", self._status(WileyBackend()))


class CookieTest(unittest.TestCase):
    """A session entitled in the user's browser is carried in as a ``Cookie`` header."""

    def test_header_string_is_parsed_and_trimmed(self):
        got = parse_cookie("Cookie: cf_clearance=abc; wiley-session=xyz;")
        self.assertEqual(got, {"cf_clearance": "abc", "wiley-session": "xyz"})

    def test_netscape_export_keeps_wiley_hosts_only(self):
        raw = ("# Netscape HTTP Cookie File\n"
               ".wiley.com\tTRUE\t/\tTRUE\t0\tWILEY_SESSION\tsess\n"
               ".example.com\tTRUE\t/\t0\t0\tOTHER\tno\n"
               ".onlinelibrary.wiley.com\tTRUE\t/\tTRUE\t0\tcf_clearance\tclr\n")
        self.assertEqual(parse_cookie(raw), {"WILEY_SESSION": "sess", "cf_clearance": "clr"})

    def test_blank_and_comments_are_skipped(self):
        self.assertEqual(parse_cookie("\n# c\n \n"), {})

    def test_backend_sends_the_cookie_and_the_user_agent(self):
        be = WileyBackend(cookie="cf_clearance=abc", user_agent="Mozilla/5.0 Mine")
        speed_up(be)
        rec = stub(be, (200, JSONLD_PAGE))
        be.get(DOI)
        self.assertEqual(rec.requests[0].headers.get("cookie"), "cf_clearance=abc")
        self.assertIn("Mine", rec.requests[0].headers.get("user-agent", ""))

    def test_a_cookie_without_a_user_agent_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            WileyBackend(cookie="cf_clearance=abc")
        self.assertIn("user_agent", str(ctx.exception))

    def test_cookie_file_is_read_and_merged_below_the_inline_value(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("WILEY_SESSION=from-file\ncf_clearance=file-clr\n")
        self.addCleanup(os.unlink, f.name)
        be = WileyBackend(cookie="cf_clearance=inline-clr", cookie_file=f.name,
                          user_agent="Mozilla/5.0 Mine")
        self.assertEqual(be.cookie, "cf_clearance=inline-clr; WILEY_SESSION=from-file")

    def test_a_missing_cookie_file_names_the_path(self):
        with self.assertRaises(RuntimeError) as ctx:
            WileyBackend(cookie_file="/nonexistent/cookies.txt")
        self.assertIn("/nonexistent/cookies.txt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
