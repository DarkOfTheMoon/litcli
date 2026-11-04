"""DOAB backend offline stub tests: Atom feed parsing, handle keys, Anubis challenge, page parsing."""
import hashlib
import json
import os
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends.doab import DoabBackend, feed_to_items, page_to_item, solve_challenge
from tests.http_stub import speed_up, stub

COOKIE = {"Set-Cookie": "techaro.lol-anubis-auth=v; Domain=.doabooks.org; Path=/"}

PAGE = """<html><head><title>Finite Volume Method</title>
<meta name="DC.creator" content="Petrova, Radostina" />
<meta name="DC.creator" content="Ivanov, Sergei" />
<meta name="DC.title" content="Finite Volume Method" /></head><body>
<div class="page-header first-page-header"><h2>Finite Volume Method</h2></div>
<div class="simple-item-view-description item-page-field-wrapper"><h5>Abstract</h5>
We hope that among these chapters.</div>
<div class="item-page-field-wrapper"><h5>Download Url(s)</h5>
<a href="https://x.test/book.pdf">https://x.test/book.pdf</a></div>
<div class="item-page-field-wrapper"><h5>Language</h5>English</div>
<div class="item-page-field-wrapper"><h5>DOI</h5><a href="https://doi.org/10.1/b">10.1/b</a></div>
<div class="item-page-field-wrapper"><h5>ISBN</h5>9789535104452, 9789535156642</div>
<div class="item-page-field-wrapper"><h5>Publisher</h5><a href="#">IntechOpen</a></div>
<div class="item-page-field-wrapper"><h5>Publication date and place</h5>2012, Rijeka</div>
<div class="item-page-field-wrapper"><h5>Pages</h5>386</div>
</body></html>"""


def _challenge(difficulty=1, random_data="abc123"):
    body = {"rules": {"algorithm": "fast", "difficulty": difficulty},
            "challenge": {"id": "cid-1", "randomData": random_data, "method": "fast"}}
    return ('<html><script id="anubis_challenge" type="application/json">'
            + json.dumps(body) + "</script></html>")


PUBLISHER_PAGE = """<html><head><title>Tirant</title>
<meta name="DC.type" content="publisher" /></head><body>
<div class="page-header first-page-header"><h2>TIRANT LO BLANCH S.L</h2></div>
<div class="item-page-field-wrapper"><h5>Website</h5><a href="https://tirant.test/">x</a></div>
</body></html>"""

PAGE_MULTI_LINKS = PAGE.replace(
    '<a href="https://x.test/book.pdf">https://x.test/book.pdf</a>',
    '<a href="https://x.test/landing">https://x.test/landing</a> '
    '<a href="https://x.test/book.pdf">https://x.test/book.pdf</a>')

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <title>Finite Volume Method</title>
  <link href="https://directory.doabooks.org/handle/20.500.12854/57156" rel="alternate"/>
  <author><name>Versteeg, Henk Kaarle</name></author>
  <id>https://directory.doabooks.org/handle/20.500.12854/57156</id>
  <published>2012-01-01T00:00:00Z</published>
  <summary type="text">A textbook on the finite volume method.</summary>
 </entry>
 <entry>
  <title></title>
  <id>https://directory.doabooks.org/handle/20.500.12854/99999</id>
 </entry>
</feed>"""

# DOAB mixes publisher/imprint handles into the feed as "Untitled" entries without a
# date or summary (they carry no DC title).
FEED_WITH_PUBLISHER = FEED.replace("</feed>", """ <entry>
  <title>Untitled</title>
  <author><name></name></author>
  <id>https://directory.doabooks.org/handle/20.500.12854/169562</id>
 </entry>
</feed>""")


class DoabStubTest(unittest.TestCase):
    def setUp(self):
        self.be = self._backend()

    @staticmethod
    def _backend():
        be = DoabBackend(delay=0)
        speed_up(be)
        return be

    def test_feed_parsing_skips_untitled_entries(self):
        items = feed_to_items(FEED.encode())
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.item_type, "book")
        self.assertEqual(it.key, "20.500.12854/57156")
        self.assertEqual(it.year, "2012")
        self.assertEqual(it.creators, [{"family": "Versteeg", "given": "Henk Kaarle"}])
        self.assertEqual(it.url, "https://directory.doabooks.org/handle/20.500.12854/57156")

    def test_feed_drops_a_publisher_placeholder_entry(self):
        items = feed_to_items(FEED_WITH_PUBLISHER.encode())
        self.assertEqual([i.key for i in items], ["20.500.12854/57156"])

    def test_missing_handle_reports_not_found(self):
        be = self._backend()
        stub(be, (403, _challenge()), (200, "", COOKIE), (404, "gone"))
        with self.assertRaises(RuntimeError) as cm:
            be.get("20.500.1")
        self.assertIn("not found", str(cm.exception))

    def test_a_set_auth_cookie_marks_the_challenge_as_solved(self):
        # pass-challenge redirects to the target; a 404 target still set the cookie and must
        # not be reported as a failed bot check.
        be = self._backend()
        stub(be, (403, _challenge()), (404, "redirect target", COOKIE), (404, "gone"))
        with self.assertRaises(RuntimeError) as cm:
            be.get("20.500.1")
        self.assertIn("not found", str(cm.exception))

    def test_persistent_403_reports_the_http_status(self):
        # Both attempts answer a fresh challenge and both solves succeed, yet the page keeps
        # refusing: the error must be the HTTP status, not the generic bot-check message.
        be = self._backend()
        stub(be, (403, _challenge()), (200, "", COOKIE), (403, _challenge()), (200, "", COOKIE))
        with self.assertRaises(RuntimeError) as cm:
            be.get("20.500.1")
        self.assertIn("HTTP 403", str(cm.exception))

    def test_get_rejects_a_publisher_handle(self):
        be = self._backend()
        stub(be, (200, PUBLISHER_PAGE))
        with self.assertRaises(RuntimeError):
            be.get("20.500.12854/169562")

    def test_page_item_records_the_dc_type(self):
        self.assertEqual(page_to_item(PUBLISHER_PAGE, "h").extra["dc_type"], "publisher")

    def test_search_filters_by_year_after_the_fact(self):
        stub(self.be, (200, FEED))
        self.assertEqual(len(self.be.search("finite volume", limit=5)), 1)
        stub(self.be, (200, FEED))
        self.assertEqual(self.be.search("finite volume", filters={"year": "1999"}), [])

    def test_search_retries_a_transient_500(self):
        rec = stub(self.be, (500, "boom"), (200, FEED))
        self.assertEqual(len(self.be.search("finite volume")), 1)
        self.assertEqual(len(rec.requests), 2)

    def test_year_filter_requests_a_deeper_page(self):
        rec = stub(self.be, (200, FEED))
        self.be.search("finite volume", filters={"year": "2012"})
        self.assertIn("rpp=100", rec.urls[0])

    def test_blank_query_browses_everything(self):
        # Like the other backends, a blank query must not be an empty result.
        stub(self.be, (200, FEED))
        self.assertEqual(len(self.be.search("  ")), 1)

    def test_search_pages_past_the_feed_page_cap(self):
        entries = "".join(
            f"<entry><title>Book {i}</title><id>https://x/handle/20.500.1/{i}</id>"
            f"<published>2020-01-01T00:00:00Z</published></entry>" for i in range(100))
        big = FEED.replace("</feed>", entries + "</feed>")
        rec = stub(self.be, (200, big))
        items = self.be.search("book", limit=150)
        self.assertEqual(len(items), 150)
        self.assertEqual(len(rec.requests), 2)
        # FEED itself contributes one entry, so the next page starts at 101.
        self.assertIn("start=101", rec.urls[1])

    def test_get_clears_the_bot_check_and_parses_the_record(self):
        be = self._backend()
        rec = stub(be, (403, _challenge()), (200, "", COOKIE), (200, PAGE))
        it = be.get("doab/20.500.12854/65919")
        self.assertEqual(it.title, "Finite Volume Method")
        self.assertEqual(it.isbn, "9789535104452")           # ISBN-13 kept, no trailing comma
        self.assertEqual(it.publisher, "IntechOpen")
        self.assertEqual(it.year, "2012")
        self.assertEqual(it.page, "386")
        self.assertEqual(it.doi, "10.1/b")
        self.assertEqual(it.attachments, ["https://x.test/book.pdf"])
        self.assertEqual(it.abstract, "We hope that among these chapters.")
        # DC.creator repeats once per person; every one must survive, not just the first.
        self.assertEqual(it.creators, [{"family": "Petrova", "given": "Radostina"},
                                       {"family": "Ivanov", "given": "Sergei"}])
        # The second call is the solved challenge; the third retries the page itself.
        nonce = next(n for n in range(5000)
                     if hashlib.sha256(("abc123" + str(n)).encode()).hexdigest().startswith("0"))
        digest = hashlib.sha256(("abc123" + str(nonce)).encode()).hexdigest()
        self.assertIn("api/pass-challenge", rec.urls[1])
        self.assertIn(f"response={digest}&nonce={nonce}", rec.urls[1])
        self.assertEqual(rec.urls[2], rec.urls[0])

    def test_get_without_title_or_isbn_raises(self):
        be = self._backend()
        stub(be, (200, "<html><body>nope</body></html>"))
        with self.assertRaises(KeyError):
            be.get("20.500.12854/57156")

    def test_get_attachment_requires_a_pdf(self):
        be = self._backend()
        stub(be, (200, PAGE), (200, "%PDF-1.7 bytes"))
        self.assertTrue(be.get_attachment("x/20.500.1").startswith(b"%PDF"))
        stub(be, (200, PAGE), (200, "<html>login</html>"))
        with self.assertRaises(RuntimeError):
            be.get_attachment("x/20.500.1")

    def test_get_attachment_falls_through_a_landing_link_to_the_pdf(self):
        be = self._backend()
        stub(be, (200, PAGE_MULTI_LINKS), (200, "<html>landing</html>"),
             (200, "%PDF-1.7 bytes"))
        self.assertTrue(be.get_attachment("20.500.1").startswith(b"%PDF"))

    def test_page_item_collects_every_download_link(self):
        it = page_to_item(PAGE_MULTI_LINKS, "h")
        self.assertEqual(it.attachments, ["https://x.test/landing", "https://x.test/book.pdf"])
        self.assertEqual(it.extra["download"], "https://x.test/landing")


class ChallengeTest(unittest.TestCase):
    def test_solution_satisfies_the_difficulty(self):
        solved = solve_challenge(_challenge(difficulty=2, random_data="seed"))
        self.assertEqual(solved["id"], "cid-1")
        digest = hashlib.sha256(("seed" + solved["nonce"]).encode()).hexdigest()
        self.assertEqual(digest, solved["response"])
        self.assertTrue(digest.startswith("00"))

    def test_zero_difficulty_accepts_the_first_nonce(self):
        self.assertEqual(solve_challenge(_challenge(difficulty=0))["nonce"], "0")

    def test_plain_page_has_no_challenge(self):
        self.assertIsNone(solve_challenge("<html>normal</html>"))

    def test_page_item_keeps_series_and_rights_out_of_the_core_fields(self):
        it = page_to_item(PAGE, "h")
        self.assertEqual(it.source, "doab")
        self.assertEqual(it.extra["language"], "English")


if __name__ == "__main__":
    unittest.main()
