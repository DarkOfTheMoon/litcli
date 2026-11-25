"""ScienceDirect backend offline tests: pure parsers and the stubbed fetch path."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends._cf_gate import is_challenge_page as is_block_page
from litcli.backends.sciencedirect import (
    ScienceDirectBackend,
    extract_pdf_url,
    normalize_key,
    page_to_item,
    parse_cookie,
)
from litcli.errors import BadInput, BotWall, Paywalled
from tests.http_stub import speed_up, stub

PII = "S0307904X14001231"
LINKINGHUB = f"https://linkinghub.elsevier.com/retrieve/pii/{PII}"

JSONLD_PAGE = """<html><head><title>ignored title</title>
<script type="application/ld+json">{
 "@type": "ScholarlyArticle",
 "headline": "A Study of Things",
 "author": [{"@type": "Person", "name": "Jane Doe"}, "John Smith"],
 "datePublished": "2021-05-01",
 "isPartOf": {"name": "Journal of Things", "volumeNumber": "12", "issueNumber": "3"},
 "pageStart": "100", "pageEnd": "110",
 "identifier": "10.1016/j.jot.2021.01.001",
 "description": "<p>An abstract of things.</p>",
 "publisher": {"name": "Elsevier"}
}</script></head><body></body></html>"""

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
<meta name="citation_doi" content="10.1016/j.meta.2019.01.002">
<meta name="citation_abstract" content="A meta abstract.">
<meta name="citation_pdf_url" content="{LINKINGHUB.replace('retrieve/pii', 'x')}">
</head><body></body></html>"""

DC_PAGE = """<html><head>
<meta name="dc.title" content="Dublin Core Title">
<meta name="dc.creator" content="Roe, Richard">
<meta name="dc.source" content="DC Journal">
<meta name="dc.date" content="2018">
<meta name="dc.identifier" content="10.1016/j.dc.2018.01.003">
</head><body></body></html>"""

BLOCK_PAGE = """<html><body><div class="block"><h1>Checking access</h1>
<p>Error CPE00001: your IP address is not entitled.</p></div></body></html>"""

PDF_PAGE = """<html><head>
<meta name="citation_title" content="PDF Carried Title">
<script>window.__DATA__ = {"pdfUrl":"https:\\/\\/pdf.sciencedirectassets.com\\/1-s2.0-"""
PDF_PAGE += PII + """\\/main.pdf?X-Amz-Signature=abc&amp;X-Amz-Expires=300"};</script>
</head><body></body></html>"""

BAD_HOST_PAGE = """<html><head>
<script>window.__DATA__ = {"pdfUrl":"https://evil.example.com/x.pdf"};</script>
</head><body></body></html>"""

NO_PDF_PAGE = JSONLD_PAGE


def _pdf_anchor(pii, md5):
    """The PDF anchor a served article page carries (real shape, from a 2026-11 capture)."""
    return (f'<a class="pdf-download" href="/science/article/pii/{pii}/pdfft?md5={md5}'
            f'&amp;pid=1-s2.0-{pii}-main.pdf">Download PDF</a>')


class NormalizeKeyTest(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(normalize_key("10.1016/j.apm.2014.03.020"),
                         "doi:10.1016/j.apm.2014.03.020")

    def test_doi_url(self):
        self.assertEqual(normalize_key("https://doi.org/10.1016/j.apm.2014.03.020"),
                         "doi:10.1016/j.apm.2014.03.020")

    def test_pii_prefix_is_upper_cased(self):
        self.assertEqual(normalize_key(f"pii:{PII.lower()}"), f"pii:{PII}")

    def test_bare_pii(self):
        self.assertEqual(normalize_key(PII), f"pii:{PII}")

    def test_article_url(self):
        self.assertEqual(normalize_key(f"https://www.sciencedirect.com/science/article/pii/{PII}"),
                         f"pii:{PII}")

    def test_abs_url(self):
        self.assertEqual(
            normalize_key(f"https://www.sciencedirect.com/science/article/abs/pii/{PII}"),
            f"pii:{PII}")

    def test_linkinghub_url(self):
        self.assertEqual(normalize_key(LINKINGHUB), f"pii:{PII}")

    def test_url_pii_separators_are_stripped(self):
        # Elsevier prints the separators in URLs; the same article is S0045-7825(05)00073-3.
        url = "https://www.sciencedirect.com/science/article/pii/S0045-7825(05)00073-3"
        self.assertEqual(normalize_key(url), "pii:S0045782505000733")
        self.assertEqual(normalize_key("S0045-7825(05)00073-3"), "pii:S0045782505000733")

    def test_a_pdf_asset_url(self):
        # The link a browser download hands you (a signed CDN URL, five minutes to live) names
        # the article by `pii=` and by its `1-s2.0-<PII>` path segment; neither is a /pii/ path.
        url = (f"https://pdf.sciencedirectassets.com/271985/1-s2.0-{PII[:14]}X00097/"
               f"1-s2.0-{PII}/am.pdf?X-Amz-Expires=300&pii={PII}&type=client")
        self.assertEqual(normalize_key(url), f"pii:{PII}")

    def test_a_pdf_asset_url_without_the_query_still_works(self):
        url = f"https://pdf.sciencedirectassets.com/271985/1-s2.0-{PII}/am.pdf"
        self.assertEqual(normalize_key(url), f"pii:{PII}")

    def test_a_doi_query_tail_is_not_part_of_the_doi(self):
        self.assertEqual(normalize_key("10.1016/j.apm.2014.03.020?download=true"),
                         "doi:10.1016/j.apm.2014.03.020")
        self.assertEqual(normalize_key("10.1016/j.apm.2014.03.020#sec2"),
                         "doi:10.1016/j.apm.2014.03.020")

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

    def test_robot_challenge_marker_detected(self):
        self.assertTrue(is_block_page("<html><title>Just a moment...</title>"
                                      "<body>Are you a robot?</body></html>"))


class PageToItemTest(unittest.TestCase):
    def test_json_ld_is_preferred(self):
        it = page_to_item(JSONLD_PAGE, f"pii:{PII}")
        self.assertEqual(it.source, "sciencedirect")
        self.assertEqual(it.key, f"pii:{PII}")
        self.assertEqual(it.title, "A Study of Things")
        self.assertEqual(it.creators, [{"family": "Doe", "given": "Jane"},
                                       {"family": "Smith", "given": "John"}])
        self.assertEqual(it.year, "2021")
        self.assertEqual(it.container_title, "Journal of Things")
        self.assertEqual(it.volume, "12")
        self.assertEqual(it.issue, "3")
        self.assertEqual(it.page, "100-110")
        self.assertEqual(it.doi, "10.1016/j.jot.2021.01.001")
        self.assertEqual(it.abstract, "An abstract of things.")
        self.assertEqual(it.publisher, "Elsevier")
        self.assertEqual(it.url, f"https://www.sciencedirect.com/science/article/pii/{PII}")
        self.assertEqual(it.item_type, "article")

    def test_citation_meta_variant(self):
        it = page_to_item(CITATION_PAGE, f"pii:{PII}")
        self.assertEqual(it.title, "Meta Carried Title")
        self.assertEqual(it.creators, [{"family": "Doe", "given": "Jane"},
                                       {"family": "Smith", "given": "John"}])
        self.assertEqual(it.container_title, "Meta Journal")
        self.assertEqual(it.year, "2019")
        self.assertEqual(it.page, "55-66")
        self.assertEqual(it.doi, "10.1016/j.meta.2019.01.002")
        self.assertEqual(it.abstract, "A meta abstract.")

    def test_dc_fallback(self):
        it = page_to_item(DC_PAGE, f"pii:{PII}")
        self.assertEqual(it.title, "Dublin Core Title")
        self.assertEqual(it.creators, [{"family": "Roe", "given": "Richard"}])
        self.assertEqual(it.container_title, "DC Journal")
        self.assertEqual(it.year, "2018")
        self.assertEqual(it.doi, "10.1016/j.dc.2018.01.003")

    def test_dc_identifier_doi_url_is_normalized(self):
        page = '<meta name="dc.title" content="T"><meta name="dc.identifier" ' \
               'content="https://doi.org/10.1016/j.x.2020.01.004">'
        self.assertEqual(page_to_item(page, "pii:X").doi, "10.1016/j.x.2020.01.004")

    def test_title_tag_is_last_resort(self):
        it = page_to_item("<html><head><title>Only a Title</title></head></html>", "pii:X")
        self.assertEqual(it.title, "Only a Title")

    def test_a_related_ld_record_does_not_rename_the_article(self):
        # A related-item card may ship its own record ahead of the article's; the meta names
        # this article, so the card's title and DOI must not overwrite it.
        page = CITATION_PAGE + (
            '<script type="application/ld+json">{"@type": "ScholarlyArticle", '
            '"headline": "RELATED: other", "identifier": "10.9999/rel"}</script>')
        it = page_to_item(page, f"pii:{PII}")
        self.assertEqual(it.title, "Meta Carried Title")
        self.assertEqual(it.doi, "10.1016/j.meta.2019.01.002")

    def test_an_ld_record_naming_the_article_is_preferred(self):
        page = ('<meta name="citation_title" content="META">'
                '<meta name="citation_doi" content="10.1016/j.same.2020.01.001">'
                '<script type="application/ld+json">{"@type": "ScholarlyArticle", '
                '"headline": "RECORD", "identifier": "10.1016/j.same.2020.01.001"}</script>')
        self.assertEqual(page_to_item(page, f"pii:{PII}").title, "RECORD")


class ExtractPdfUrlTest(unittest.TestCase):
    def _page(self, *piis, own_first=False):
        links = "".join(
            f'<a class="pdf-download" href="/science/article/pii/{p}/pdfft?md5={m}'
            f'&amp;pid=1-s2.0-{p}-main.pdf">Download</a>'
            for p, m in piis)
        return f"<html><body>{links}</body></html>"

    def test_pdfft_anchor_of_the_article_is_taken(self):
        # The served page carries no pdfUrl; entitled articles show a pdfft anchor instead.
        page = self._page((PII, "a" * 32))
        self.assertEqual(extract_pdf_url(page, PII),
                         f"https://www.sciencedirect.com/science/article/pii/{PII}"
                         f"/pdfft?md5={'a' * 32}&pid=1-s2.0-{PII}-main.pdf")

    def test_unrelated_pdfft_cards_are_ignored(self):
        # Related-article cards carry pdfft links too; only the article's own PII counts.
        page = self._page(("S0045782599002005", "b" * 32), ("S0168874X01000518", "c" * 32))
        self.assertEqual(extract_pdf_url(page, PII), "")

    def test_without_a_pii_the_first_pdfft_is_a_fallback(self):
        page = self._page(("S0045782599002005", "b" * 32))
        self.assertIn("/pii/S0045782599002005/pdfft", extract_pdf_url(page))

    def test_pdfft_pii_separators_still_match(self):
        page = self._page(("S0045782505000733", "d" * 32))
        self.assertIn("pdfft", extract_pdf_url(page, "S0045-7825(05)00073-3"))

    def test_script_embedded_escaped_url(self):
        url = extract_pdf_url(PDF_PAGE)
        self.assertEqual(
            url, "https://pdf.sciencedirectassets.com/1-s2.0-" + PII
            + "/main.pdf?X-Amz-Signature=abc&X-Amz-Expires=300")

    def test_a_related_pdfurl_is_not_taken_for_the_article(self):
        # The asset URL embeds the PII; a related card's ``pdfUrl`` names another one.
        page = ('<script>{"pdfUrl": "https://pdf.sciencedirectassets.com/1-s2.0-'
                'S0045782599002005/main.pdf?a=1"}</script>'
                f'<script>{{"pdfUrl": "https://pdf.sciencedirectassets.com/1-s2.0-{PII}'
                '/main.pdf?a=2"}</script>')
        self.assertIn(PII, extract_pdf_url(page, PII))

    def test_citation_pdf_meta(self):
        page = ('<meta name="citation_pdf_url" '
                'content="https://www.sciencedirect.com/science/article/pii/X/pdfft">')
        self.assertEqual(extract_pdf_url(page),
                         "https://www.sciencedirect.com/science/article/pii/X/pdfft")

    def test_the_open_manuscript_link_is_a_pdf_link(self):
        # A bronze / open-archive article serves its accepted manuscript at ``/am/pii/<PII>``,
        # which redirects to the file; the served page carries that link and no pdfft of its
        # own, so refusing it leaves an openly readable article with no copy.
        page = (f'<a href="/science/article/am/pii/{PII}" target="_blank" '
                'aria-label="View Open Manuscript">View Open Manuscript</a>')
        self.assertEqual(extract_pdf_url(page, PII),
                         f"https://www.sciencedirect.com/science/article/am/pii/{PII}")

    def test_another_articles_open_manuscript_is_ignored(self):
        page = ('<a href="/science/article/am/pii/S0045782599002005" '
                'aria-label="View Open Manuscript">View Open Manuscript</a>')
        self.assertEqual(extract_pdf_url(page, PII), "")

    def test_a_pdfft_anchor_wins_over_the_open_manuscript(self):
        # The pdfft is the version of record; the manuscript is the fallback, not the target.
        page = (self._page((PII, "a" * 32))
                + f'<a href="/science/article/am/pii/{PII}">View Open Manuscript</a>')
        self.assertIn("/pdfft?", extract_pdf_url(page, PII))

    def test_missing_returns_empty(self):
        self.assertEqual(extract_pdf_url(NO_PDF_PAGE), "")

    def test_bad_host_raises(self):
        with self.assertRaises(RuntimeError):
            extract_pdf_url(BAD_HOST_PAGE)


class ScienceDirectBackendTest(unittest.TestCase):
    def setUp(self):
        self.be = ScienceDirectBackend()
        speed_up(self.be)
        # A sessioned instance: without a session the refusal is always "no clearance cookie",
        # which would hide the second-gate wording ScienceDirect turns an HTML answer into.
        self.sessioned = ScienceDirectBackend(cookie="sessionid=abc", user_agent="UA/1")
        speed_up(self.sessioned)

    def test_capabilities_are_read_only(self):
        self.assertEqual(self.be.capabilities, {"read"})

    def test_access_is_institutional_not_oa(self):
        self.assertEqual(self.be.access, "entitled")

    def test_get_puts_the_pdf_url_in_attachments(self):
        stub(self.be, (200, PDF_PAGE))
        it = self.be.get(PII)
        self.assertEqual(len(it.attachments), 1)
        self.assertIn("pdf.sciencedirectassets.com", it.attachments[0])

    def test_get_without_a_pdf_link_has_no_attachments(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.get(PII).attachments, [])

    def test_get_attaches_the_articles_own_pdfft_link_only(self):
        page = DC_PAGE + _pdf_anchor("S0045782599002005", "b" * 32) + _pdf_anchor(PII, "a" * 32)
        stub(self.be, (200, page))
        item = self.be.get(PII)
        self.assertEqual(len(item.attachments), 1)
        self.assertIn(f"/pii/{PII}/pdfft", item.attachments[0])

    def test_get_on_a_page_with_only_related_pdfft_cards_has_no_attachments(self):
        page = DC_PAGE + _pdf_anchor("S0045782599002005", "b" * 32)
        stub(self.be, (200, page))
        self.assertEqual(self.be.get(PII).attachments, [])

    def test_get_by_pii_fetches_the_article_url(self):
        rec = stub(self.be, (200, JSONLD_PAGE))
        it = self.be.get(PII)
        self.assertEqual(rec.urls[0],
                         f"https://www.sciencedirect.com/science/article/pii/{PII}")
        self.assertEqual(it.key, f"pii:{PII}")

    def test_get_accepts_an_article_url(self):
        stub(self.be, (200, JSONLD_PAGE))
        it = self.be.get(f"https://www.sciencedirect.com/science/article/pii/{PII}")
        self.assertEqual(it.title, "A Study of Things")

    def test_get_resolves_a_doi_through_doi_org(self):
        rec = stub(self.be, (302, "", {"Location": LINKINGHUB}), (200, JSONLD_PAGE))
        it = self.be.get("10.1016/j.apm.2014.03.020")
        self.assertEqual(rec.urls[0], "https://doi.org/10.1016/j.apm.2014.03.020")
        self.assertEqual(rec.urls[1],
                         f"https://www.sciencedirect.com/science/article/pii/{PII}")
        self.assertEqual(it.key, f"pii:{PII}")

    def test_doi_resolving_off_site_is_not_found(self):
        stub(self.be, (302, "", {"Location": "https://example.org/x"}))
        with self.assertRaises(Exception) as ctx:
            self.be.get("10.1016/j.elsewhere")
        self.assertIn("does not resolve", str(ctx.exception))

    def test_403_block_page_raises_a_named_error(self):
        stub(self.be, (403, BLOCK_PAGE))
        with self.assertRaises(BotWall) as ctx:
            self.be.get(PII)
        self.assertIn("CPE00001", str(ctx.exception))
        self.assertIn("session", str(ctx.exception))

    def test_200_block_marker_raises(self):
        stub(self.be, (200, BLOCK_PAGE))
        with self.assertRaises(BotWall):
            self.be.get(PII)

    def test_cloudflare_challenge_marker_raises(self):
        stub(self.be, (200, "<html><body>Enable JavaScript and cookies to continue</body></html>"))
        with self.assertRaises(BotWall):
            self.be.get(PII)

    def test_cloudflare_mitigated_header_raises(self):
        stub(self.be, (200, JSONLD_PAGE, {"cf-mitigated": "challenge"}))
        with self.assertRaises(BotWall):
            self.be.get(PII)

    def test_5xx_retries_then_succeeds(self):
        rec = stub(self.be, (503, "busy"), (200, JSONLD_PAGE))
        self.assertEqual(self.be.get(PII).title, "A Study of Things")
        self.assertEqual(len(rec.requests), 2)

    def test_a_sign_in_page_is_not_accepted_as_the_item(self):
        # The site answers the sign-in wall with a 200: it must not become an item titled
        # "Sign in to …". This ``get`` is its own override, so the guard is called here too.
        stub(self.be, (200, "<html><head><title>Sign in to ScienceDirect</title></head>"
                            '<body><a href="/login">Sign in</a></body></html>'))
        with self.assertRaises(BotWall) as ctx:
            self.be.get(PII)
        self.assertIn("sign-in page", str(ctx.exception))

    def test_unparseable_page_raises(self):
        stub(self.be, (200, "<html><body></body></html>"))
        with self.assertRaises(RuntimeError):
            self.be.get(PII)

    def test_search_keyword_returns_empty_without_a_request(self):
        rec = stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.search("finite volume method"), [])
        self.assertEqual(rec.urls, [])

    def test_search_pii_returns_the_single_item(self):
        stub(self.be, (200, JSONLD_PAGE))
        items = self.be.search(PII, limit=5)
        self.assertEqual([i.key for i in items], [f"pii:{PII}"])

    def test_search_zero_limit_is_empty(self):
        stub(self.be, (200, JSONLD_PAGE))
        self.assertEqual(self.be.search(PII, limit=0), [])

    def test_search_doi_returns_the_single_item(self):
        stub(self.be, (302, "", {"Location": LINKINGHUB}), (200, JSONLD_PAGE))
        items = self.be.search("10.1016/j.apm.2014.03.020")
        self.assertEqual([i.key for i in items], [f"pii:{PII}"])

    def test_get_attachment_downloads_the_pdf(self):
        rec = stub(self.be, (200, PDF_PAGE), (200, b"%PDF-1.7 bytes"))
        data = self.be.get_attachment(PII)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn("pdf.sciencedirectassets.com", rec.urls[1])

    def test_get_attachment_without_a_pdf_link_raises(self):
        stub(self.be, (200, NO_PDF_PAGE))
        with self.assertRaises(Paywalled) as ctx:
            self.be.get_attachment(PII)
        self.assertIn("no PDF link", str(ctx.exception))

    def test_get_attachment_non_pdf_body_raises(self):
        stub(self.sessioned, (200, PDF_PAGE), (200, "<html>login</html>"))
        with self.assertRaises(Exception) as ctx:
            self.sessioned.get_attachment(PII)
        self.assertIn("no PDF bytes", str(ctx.exception))
        self.assertIn("second ScienceDirect acceptance gate", str(ctx.exception))

    def test_without_a_session_the_refusal_names_the_missing_cookie(self):
        stub(self.be, (200, PDF_PAGE), (200, "<html>login</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(PII)
        self.assertIn("no clearance cookie", str(ctx.exception))

    def test_get_attachment_from_a_pdfft_link(self):
        rec = stub(self.be, (200, DC_PAGE + _pdf_anchor(PII, "a" * 32)), (200, b"%PDF-1.7 bytes"))
        data = self.be.get_attachment(PII)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn(f"/pii/{PII}/pdfft", rec.urls[1])

    def test_get_attachment_403_is_reported_as_the_pdf_gate(self):
        stub(self.be, (200, DC_PAGE + _pdf_anchor(PII, "a" * 32)), (403, "<html>challenge</html>"))
        with self.assertRaises(Exception) as ctx:
            self.be.get_attachment(PII)
        self.assertIn("second ScienceDirect acceptance gate", str(ctx.exception))

    def test_get_then_attachment_reuses_the_page(self):
        rec = stub(self.be, (200, PDF_PAGE), (200, b"%PDF-1.7 bytes"))
        self.be.get(PII)
        self.be.get_attachment(PII)
        self.assertEqual(len(rec.urls), 2)  # the page and the PDF, not the page twice
        self.assertIn("main.pdf", rec.urls[1])


class CookieTest(unittest.TestCase):
    """A session entitled in the user's browser is carried in as a ``Cookie`` header."""

    def test_header_string_is_parsed_and_trimmed(self):
        got = parse_cookie("Cookie: cf_clearance=abc; SD_SESSION=xyz;")
        self.assertEqual(got, {"cf_clearance": "abc", "SD_SESSION": "xyz"})

    def test_netscape_export_keeps_elsevier_hosts_only(self):
        raw = ("# Netscape HTTP Cookie File\n"
               ".sciencedirect.com\tTRUE\t/\tTRUE\t0\tSD_SESSION\tsess\n"
               ".example.com\tTRUE\t/\tTRUE\t0\tOTHER\tno\n"
               ".elsevier.com\tTRUE\t/\tTRUE\t0\tcf_clearance\tclr\n")
        self.assertEqual(parse_cookie(raw), {"SD_SESSION": "sess", "cf_clearance": "clr"})

    def test_blank_and_comments_are_skipped(self):
        self.assertEqual(parse_cookie("\n# c\n \n"), {})

    def test_user_agent_override_rides_along(self):
        be = ScienceDirectBackend(cookie="a=b", user_agent="Mozilla/5.0 (X11; Linux x86_64) Mine")
        speed_up(be)
        rec = stub(be, (200, JSONLD_PAGE))
        be.get(PII)
        self.assertIn("Mine", rec.requests[0].headers.get("user-agent", ""))

    def test_backend_sends_the_cookie(self):
        be = ScienceDirectBackend(cookie="cf_clearance=abc", user_agent="Mozilla/5.0 Mine")
        speed_up(be)
        rec = stub(be, (200, JSONLD_PAGE))
        be.get(PII)
        # The first hop is ``doi.org`` (no cookie of its own); the publisher hop carries it.
        self.assertEqual(rec.cookies[-1], "cf_clearance=abc")
        self.assertIn("sciencedirect.com", rec.urls[-1])

    def test_cookie_file_is_read_and_merged_below_the_inline_value(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("SD_SESSION=from-file\ncf_clearance=file-clr\n")
        self.addCleanup(os.unlink, f.name)
        be = ScienceDirectBackend(cookie="cf_clearance=inline-clr", cookie_file=f.name,
                                  user_agent="Mozilla/5.0 Mine")
        self.assertEqual(be.cookie, "cf_clearance=inline-clr; SD_SESSION=from-file")

    def test_a_cookie_without_a_user_agent_is_rejected(self):
        # The clearance cookie is bound to the browser's UA (and egress IP); a cookie alone
        # can only ever be useless, so say so at construction instead of a mystery 403 later.
        with self.assertRaises(RuntimeError) as ctx:
            ScienceDirectBackend(cookie="cf_clearance=abc")
        self.assertIn("user_agent", str(ctx.exception))

    def test_a_missing_cookie_file_names_the_path(self):
        with self.assertRaises(RuntimeError) as ctx:
            ScienceDirectBackend(cookie_file="/nonexistent/cookies.txt")
        self.assertIn("/nonexistent/cookies.txt", str(ctx.exception))

    def _status(self, be):
        import contextlib
        import io
        from unittest import mock

        from litcli.commands import diagnostics

        buf = io.StringIO()
        with mock.patch.object(diagnostics, "get_all_backends", return_value={"sciencedirect": be}), \
             contextlib.redirect_stdout(buf):
            diagnostics.cmd_backends_status({}, None)
        return buf.getvalue()

    def test_status_reports_whether_a_cookie_is_configured(self):
        with_cookie = ScienceDirectBackend(cookie="a=b", user_agent="Mozilla/5.0 Mine")
        self.assertIn("session cookie configured", self._status(with_cookie))
        self.assertIn("no session cookie", self._status(ScienceDirectBackend()))


if __name__ == "__main__":
    unittest.main()
