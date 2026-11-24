"""The six guarded publisher readers, driven from one site table.

All six share :mod:`litcli.backends._gated`; each test here asserts the site facts a subclass
actually supplies (its key shapes, its PDF path, its gate wording) over the shared behaviour,
so a copy-paste slip in one module shows up as that site's failure.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.backends import acm, aiaa, aip, aps, ieee, iop, tandf
from litcli.backends._cf_gate import is_waf_page
from litcli.errors import BadInput, BotWall, NotFound, Paywalled, Unavailable
from tests.http_stub import speed_up, stub


class Site:
    """One publisher's facts: module, backend class, keys and the pages that exercise them."""

    def __init__(self, name, module, backend, doi, page_url, pdf_url):
        self.name = name
        self.module = module
        self.backend = backend
        self.doi = doi
        self.page_url = page_url
        self.pdf_url = pdf_url

    def page(self, pdf=True, oa=False):
        meta = [f'<meta name="citation_title" content="{self.name} title">',
                f'<meta name="citation_doi" content="{self.doi}">']
        if pdf:
            meta.append(f'<meta name="citation_pdf_url" content="{self.pdf_url}">')
        if oa:
            meta.append('<div class="creative-commons__description">This is an Open Access '
                        'article.</div>')
        return "<html><head>" + "".join(meta) + "</head><body></body></html>"


SITES = (
    Site("acm", acm, acm.AcmBackend, "10.1145/3290605.3300233",
         "https://dl.acm.org/doi/10.1145/3290605.3300233",
         "https://dl.acm.org/doi/pdf/10.1145/3290605.3300233"),
    Site("aps", aps, aps.ApsBackend, "10.1103/PhysRevLett.116.061102",
         "https://link.aps.org/doi/10.1103/PhysRevLett.116.061102",
         "https://link.aps.org/doi/pdf/10.1103/PhysRevLett.116.061102"),
    Site("iop", iop, iop.IopBackend, "10.1088/1674-1137/aea10c",
         "https://iopscience.iop.org/article/10.1088/1674-1137/aea10c",
         "https://iopscience.iop.org/article/10.1088/1674-1137/aea10c/pdf"),
    Site("tandf", tandf, tandf.TandfBackend, "10.1080/17460441.2026.2762809",
         "https://www.tandfonline.com/doi/full/10.1080/17460441.2026.2762809",
         "https://www.tandfonline.com/doi/pdf/10.1080/17460441.2026.2762809"),
    Site("aiaa", aiaa, aiaa.AiaaBackend, "10.2514/6.2018-1756",
         "https://arc.aiaa.org/doi/10.2514/6.2018-1756",
         "https://arc.aiaa.org/doi/pdf/10.2514/6.2018-1756"),
)

#: The WAF page the site answers a JS-less client (empty body, 202 in the real response).
WAF_PAGE = '<html><head><script>window.gokuProps = {"key":"x"};</script></head></html>'
#: The same gate from F5's traffic shield: served *instead of* the page, with a 200 (Xplore).
TSPD_PAGE = ("<html><APM_DO_NOT_TOUCH>\n<script src=\"/TSPD/0807dc11?type=9\"></script>"
             "<head><title>Ieee title | IEEE Journals</title></head><body></body></html>")
BLOCK_PAGE = "<html><head><title>Just a moment...</title></head></html>"


class KeyShapeTest(unittest.TestCase):
    def test_a_doi_normalizes(self):
        for s in SITES:
            with self.subTest(s.name):
                self.assertEqual(s.module.normalize_key(s.doi), f"doi:{s.doi}")

    def test_the_prefixed_and_source_forms_normalize(self):
        for s in SITES:
            with self.subTest(s.name):
                self.assertEqual(s.module.normalize_key(f"{s.name}:{s.doi}"), f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key(f"{s.name}/{s.doi}"), f"doi:{s.doi}")

    def test_an_item_url_yields_its_embedded_doi(self):
        for s in SITES:
            with self.subTest(s.name):
                self.assertEqual(s.module.normalize_key(s.page_url), f"doi:{s.doi}")

    def test_a_bare_prefix_reports_the_documented_forms(self):
        for s in SITES:
            with self.subTest(s.name):
                with self.assertRaises(BadInput) as ctx:
                    s.module.normalize_key(f"{s.name}:")
                self.assertIn(f"{s.name}:<DOI>", str(ctx.exception))

    def test_a_trailing_slash_or_comma_is_not_part_of_the_key(self):
        # A pasted URL (or a DOI out of running text) carries the separator along; ``doi.org``
        # does not know it, and it stops the key equalling the page's own ``citation_doi``.
        for s in SITES:
            with self.subTest(s.name):
                self.assertEqual(s.module.normalize_key(s.page_url + "/"), f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key(s.doi + ","), f"doi:{s.doi}")

    def test_another_sites_url_is_rejected(self):
        for s in SITES:
            with self.subTest(s.name), self.assertRaises(BadInput):
                s.module.normalize_key("https://example.org/x")

    def test_a_query_fragment_or_bracket_does_not_join_the_doi(self):
        # A pasted link or a DOI out of running text drags a tail along; ``doi.org`` does not
        # know it, and it would end up percent-encoded in the URL.
        for s in SITES:
            with self.subTest(s.name):
                self.assertEqual(s.module.normalize_key(s.doi + "?download=true"),
                                 f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key(s.doi + "#sec2"), f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key(s.page_url + "?download=true"),
                                 f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key("(" + s.doi + ")"), f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key(s.doi + "}"), f"doi:{s.doi}")

    def test_a_percent_encoded_doi_in_the_path_is_decoded(self):
        # The TDM API and some links write the DOI slash as ``%2F``: it must not stay encoded.
        for s in SITES:
            with self.subTest(s.name):
                encoded = s.doi.replace("/", "%2F", 1)
                self.assertEqual(s.module.normalize_key(s.page_url.replace(s.doi, encoded)),
                                 f"doi:{s.doi}")
                self.assertEqual(s.module.normalize_key(encoded), f"doi:{s.doi}")

    def test_acm_full_html_url_yields_its_doi(self):
        self.assertEqual(
            acm.normalize_key("https://dl.acm.org/doi/fullHtml/10.1145/3290605.3300233"),
            "doi:10.1145/3290605.3300233")


class PageParseTest(unittest.TestCase):
    def test_the_page_parses_and_names_its_pdf(self):
        for s in SITES:
            with self.subTest(s.name):
                page = s.page()
                item = s.module.page_to_item(page, f"doi:{s.doi}", s.page_url)
                self.assertEqual(item.title, f"{s.name} title")
                self.assertEqual(item.doi, s.doi)
                self.assertEqual(s.module.extract_pdf_url(page), s.pdf_url)

    def test_a_trailing_separator_on_the_meta_doi_is_dropped(self):
        page = ('<meta name="citation_title" content="t">'
                '<meta name="citation_doi" content="10.1145/3290605.3300233/">')
        self.assertEqual(acm.page_to_item(page, "url:x").doi, "10.1145/3290605.3300233")

    def test_an_off_host_pdf_link_raises(self):
        for s in SITES:
            with self.subTest(s.name):
                page = '<meta name="citation_pdf_url" content="https://evil.example.com/x.pdf">'
                with self.assertRaises(RuntimeError):
                    s.module.extract_pdf_url(page)

    def test_a_page_without_a_link_has_no_pdf(self):
        for s in SITES:
            with self.subTest(s.name):
                self.assertEqual(s.module.extract_pdf_url(s.page(pdf=False)), "")

    def test_an_apostrophe_inside_a_meta_value_is_content(self):
        # The value must end at the quote that opened it: an apostrophe is not the delimiter.
        page = ('<meta name="citation_title" content="On Schrödinger\'s cat">'
                '<meta name="citation_author" content="O\'Brien, Kate">')
        item = acm.page_to_item(page, "url:x")
        self.assertEqual(item.title, "On Schrödinger's cat")
        self.assertEqual(item.creators, [{"family": "O'Brien", "given": "Kate"}])

    def test_the_aps_title_tag_tail_is_stripped(self):
        # A library-platform title tag is "<title> | <journal> | <site>"; neither belongs in the
        # title.
        page = ("<html><head><title>Some Title | Physics of Plasmas | AIP Publishing"
                "</title></head></html>")
        self.assertEqual(aps.page_to_item(page, "url:x").title, "Some Title")

    def test_a_subtitle_segment_survives_the_tail_strip(self):
        # The tail strip used to be greedy and ate "| Part II" together with the journal and
        # site segments; a subtitle is content and cannot be recovered once dropped.
        page = ("<html><head><title>On the cascade | Part II | Phys. Rev. Lett. | APS"
                "</title></head></html>")
        self.assertEqual(aps.page_to_item(page, "url:x").title, "On the cascade | Part II")
        page = ("<html><head><title>A study | Part 2 | J. Fluid Mech. | AIP Publishing"
                "</title></head></html>")
        self.assertEqual(aip.page_to_item(page, "url:x").title, "A study | Part 2")

    def test_a_card_whose_doi_extends_this_one_does_not_win(self):
        # ``10.1145/3290605`` is a prefix of the card's ``10.1145/3290605.3300233``; a dot and
        # more digits after the match mean "inside a longer DOI", not "this item".
        page = ('<a href="/doi/pdf/10.1145/3290605.3300233">card</a>'
                '<a href="/doi/pdf/10.1145/3290605">this</a>')
        self.assertEqual(acm.extract_pdf_url(page, "10.1145/3290605"),
                         "https://dl.acm.org/doi/pdf/10.1145/3290605")


class OpenAccessMarkerTest(unittest.TestCase):
    """The licence block is the marker — its class alone is carried by related cards too."""

    def test_the_licence_block_is_open_access(self):
        for s in SITES:
            with self.subTest(s.name):
                self.assertTrue(s.module.is_open_access(s.page(oa=True)))

    def test_a_card_class_without_licence_wording_is_not_open_access(self):
        for s in SITES:
            with self.subTest(s.name):
                page = s.page() + '<div class="creative-commons__description">CC BY</div>'
                self.assertFalse(s.module.is_open_access(page))

    def test_the_site_badge_class_is_open_access_everywhere(self):
        # IEEE/IOP stamp this class on the document's badge; the marker set is shared, so every
        # reader must agree on it.
        for s in SITES:
            with self.subTest(s.name):
                self.assertTrue(s.module.is_open_access('<span class="badge open-access">OA</span>'))

    def test_the_nav_wording_alone_is_not_open_access_everywhere(self):
        # The nav says "Open Access" on every page, paywalled ones included, and carries no class.
        for s in SITES:
            with self.subTest(s.name):
                self.assertFalse(s.module.is_open_access('<nav><a href="/oa">Open Access</a></nav>'))


class BackendTest(unittest.TestCase):
    def make(self, s):
        be = s.backend()
        speed_up(be)
        return be

    def test_capabilities_and_access(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                self.assertEqual(be.capabilities, {"read"})
                self.assertEqual(be.access, "entitled")

    def test_a_doi_goes_through_doi_org_and_lands_on_the_site(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                rec = stub(be, (302, "", {"location": s.page_url}), (200, s.page()))
                self.assertEqual(be.get(s.doi).title, f"{s.name} title")
                self.assertTrue(rec.urls[0].startswith("https://doi.org/"))
                self.assertEqual(rec.urls[-1], s.page_url)

    def test_the_pdf_link_reaches_attachments(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()))
                self.assertEqual(be.get(s.doi).attachments, [s.pdf_url])

    def test_an_item_url_is_normalized_to_its_doi(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                rec = stub(be, (302, "", {"location": s.page_url}), (200, s.page()))
                self.assertEqual(be.get(s.page_url).key, f"doi:{s.doi}")
                self.assertEqual(rec.urls[0], f"https://doi.org/{s.doi}")

    def test_an_open_access_page_is_marked(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page(oa=True)))
                item = be.get(s.doi)
                self.assertTrue(item.extra.get("open_access"))
                self.assertEqual(item.access, "oa")

    def test_404_is_not_found(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (404, "<html>not found</html>"))
                with self.assertRaises(NotFound):
                    be.get(s.doi)

    def test_a_sign_in_page_is_not_accepted_as_the_item(self):
        # The site answers the sign-in wall with a 200: it must not become an item titled
        # "Sign in to …".
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                page = (f"<html><head><title>Sign in to {s.name}</title></head>"
                        '<body><a href="/signin">Sign in</a></body></html>')
                stub(be, (302, "", {"location": s.page_url}), (200, page))
                with self.assertRaises(BotWall) as ctx:
                    be.get(s.doi)
                self.assertIn("sign-in page", str(ctx.exception))

    def test_a_normal_page_linking_sign_in_is_still_an_item(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                page = s.page().replace("</head>", '<a href="/signin">Sign in</a></head>')
                stub(be, (302, "", {"location": s.page_url}), (200, page))
                self.assertEqual(be.get(s.doi).title, f"{s.name} title")

    def test_a_doi_landing_elsewhere_is_not_found(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": "https://example.org/x"}), (200, s.page()))
                with self.assertRaises(NotFound):
                    be.get(s.doi)

    def test_a_challenge_becomes_the_gate_message(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (403, BLOCK_PAGE))
                with self.assertRaises(BotWall) as ctx:
                    be.get(s.doi)
                self.assertIn(s.name if s.name != "tandf" else "Taylor & Francis",
                              str(ctx.exception))
                self.assertIn("cookie_file", str(ctx.exception))

    def test_search_resolves_a_doi_and_ignores_a_word(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()))
                self.assertEqual(len(be.search(s.doi)), 1)
                self.assertEqual(be.search("a word"), [])

    def test_get_attachment_returns_the_bytes(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()),
                     (200, b"%PDF-1.7 bytes"))
                self.assertTrue(be.get_attachment(s.doi).startswith(b"%PDF"))

    def test_an_html_answer_without_a_session_names_the_missing_cookie(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()),
                     (200, "<html>the page again</html>"))
                with self.assertRaises(Paywalled) as ctx:
                    be.get_attachment(s.doi)
                self.assertIn("no clearance cookie", str(ctx.exception))

    def test_an_html_answer_with_a_session_names_the_entitlement(self):
        for s in SITES:
            with self.subTest(s.name):
                be = s.backend(cookie="cf_clearance=abc", user_agent="UA/1")
                speed_up(be)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()),
                     (200, "<html>the page again</html>"))
                with self.assertRaises(Paywalled) as ctx:
                    be.get_attachment(s.doi)
                self.assertIn("not entitled", str(ctx.exception))

    def test_a_gated_pdf_download(self):
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()),
                     (403, BLOCK_PAGE))
                with self.assertRaises(Unavailable) as ctx:
                    be.get_attachment(s.doi)
                self.assertIn("gated", str(ctx.exception))

    def test_a_challenge_served_with_a_200_is_still_a_gate(self):
        # Cloudflare also serves the interstitial with a 200: not a PDF, but not a refusal either.
        for s in SITES:
            with self.subTest(s.name):
                be = self.make(s)
                stub(be, (302, "", {"location": s.page_url}), (200, s.page()),
                     (200, BLOCK_PAGE))
                with self.assertRaises(Unavailable) as ctx:
                    be.get_attachment(s.doi)
                self.assertIn("gated", str(ctx.exception))

    def test_a_cookie_without_a_user_agent_is_refused(self):
        for s in SITES:
            with self.subTest(s.name):
                with self.assertRaises(RuntimeError) as ctx:
                    s.backend(cookie="a=1")
                self.assertIn("user_agent", str(ctx.exception))

    def test_the_cookie_reaches_the_hop_after_the_doi_org_redirect(self):
        # httpx drops the cookie header on a redirect and rebuilds it from the jar: doi.org
        # stays anonymous and the publisher hop carries the cookie, or the article request
        # would arrive at the gate unauthenticated.
        for s in SITES:
            with self.subTest(s.name):
                be = s.backend(cookie="cf_clearance=abc", user_agent="Mozilla/5.0 Mine")
                rec = stub(be, (302, "", {"location": s.page_url}), (200, s.page()))
                be.get(s.doi)
                self.assertEqual(rec.cookies, [None, "cf_clearance=abc"])

    def test_the_seed_hosts_are_the_exact_site_hosts(self):
        # A jar entry on the parent zone as well would send the cookie twice; a legitimately
        # separate host must survive the dedup.
        for s in SITES:
            with self.subTest(s.name):
                be = s.backend(cookie="cf_clearance=abc", user_agent="Mozilla/5.0 Mine")
                self.assertEqual(len(be.seed_hosts), 1)
                self.assertIn(be.seed_hosts[0].split(".")[0], s.page_url)

    def test_a_multi_host_site_keeps_its_separate_hosts_only(self):
        from litcli.backends import sciencedirect, springer

        expected = {aip.AipBackend: {"pubs.aip.org", "scitation.org"},
                    sciencedirect.ScienceDirectBackend: {"www.sciencedirect.com",
                                                         "linkinghub.elsevier.com"},
                    springer.SpringerBackend: {"link.springer.com", "www.nature.com",
                                               "biomedcentral.com"}}
        for cls, hosts in expected.items():
            with self.subTest(cls.__name__):
                be = cls(cookie="cf_clearance=abc", user_agent="Mozilla/5.0 Mine")
                self.assertEqual(set(be.seed_hosts), hosts)

    def test_only_the_sites_own_cookie_rows_are_kept(self):
        hosts = {"acm": "dl.acm.org", "aps": "link.aps.org", "iop": "iopscience.iop.org",
                 "tandf": "www.tandfonline.com", "aiaa": "arc.aiaa.org"}
        for s in SITES:
            with self.subTest(s.name):
                raw = f"{hosts[s.name]}\tTRUE\t/\tTRUE\t0\tcf_clearance\tabc\n.example\ty"
                self.assertEqual(s.module.parse_cookie(raw), {"cf_clearance": "abc"})

    def test_the_host_is_matched_on_a_label_boundary(self):
        # ``evilacm.org`` also contains ``acm.org``, as a substring but not as a label.
        self.assertEqual(acm.parse_cookie("evilacm.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tx"), {})

    def test_an_httponly_row_is_not_a_comment(self):
        # A browser export marks HttpOnly cookies (``cf_clearance`` among them) with a prefix.
        raw = ("# Netscape HTTP Cookie File\n"
               "#HttpOnly_.acm.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tabc")
        self.assertEqual(acm.parse_cookie(raw), {"cf_clearance": "abc"})

    def test_a_marker_row_without_columns_is_still_read(self):
        # The marker says "cookie row", so a bare ``k=v`` after it must not be dropped.
        self.assertEqual(acm.parse_cookie("#HttpOnly_cf=wrong"), {"cf": "wrong"})

    def test_a_space_separated_marker_row_is_read_as_columns(self):
        raw = "#HttpOnly_.dl.acm.org TRUE / TRUE 0 cf_clearance abc"
        self.assertEqual(acm.parse_cookie(raw), {"cf_clearance": "abc"})

    def test_the_exact_host_outranks_a_sibling_subdomain(self):
        # A clearance cookie is issued per exact hostname: the file order must not decide.
        raw = ("journals.aps.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tWRONG\n"
               "link.aps.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tRIGHT")
        self.assertEqual(aps.parse_cookie(raw), {"cf_clearance": "RIGHT"})

    def test_a_parent_zone_row_does_not_shadow_the_exact_host(self):
        # ``aps.org`` is also an accepted row; the ``link.aps.org`` cookie is the one that lands.
        raw = ("aps.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tPARENT_WRONG\n"
               "link.aps.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tRIGHT")
        self.assertEqual(aps.parse_cookie(raw), {"cf_clearance": "RIGHT"})

    def test_a_deeper_row_outranks_the_parent_zone_row(self):
        raw = ("aps.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tPARENT_WRONG\n"
               "journals.aps.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tDEEPER")
        self.assertEqual(aps.parse_cookie(raw), {"cf_clearance": "DEEPER"})


class IopPathTest(unittest.TestCase):
    """An IOP DOI suffix carries slashes: the PDF anchor must be read whole, not cut short."""

    def test_a_slashed_doi_suffix_still_finds_the_pdf_path(self):
        href = '<a href="/article/10.1088/1674-1137/aea10c/pdf">PDF</a>'
        self.assertEqual(iop.extract_pdf_url(href),
                         "https://iopscience.iop.org/article/10.1088/1674-1137/aea10c/pdf")

    def test_a_meta_subpage_anchor_falls_back_to_the_template(self):
        page = '<a href="/article/10.1088/1742-6596/1382/1/012030/meta">meta</a>'
        self.assertEqual(iop.extract_pdf_url(page, "10.1088/1742-6596/1382/1/012030"),
                         "https://iopscience.iop.org/article/10.1088/1742-6596/1382/1/012030/pdf")

    def test_a_related_card_anchor_does_not_win(self):
        # Related-item cards carry the same path shape; only the anchor naming this DOI counts.
        page = ('<a href="/article/10.1088/1742-6596/1/1/011001/pdf">other</a>'
                '<a href="/article/10.1088/1742-6596/1382/1/012030/pdf">this</a>')
        self.assertEqual(iop.extract_pdf_url(page, "10.1088/1742-6596/1382/1/012030"),
                         "https://iopscience.iop.org/article/10.1088/1742-6596/1382/1/012030/pdf")


class IeeeTest(unittest.TestCase):
    """IEEE Xplore: an AWS WAF challenge and an arnumber URL instead of a DOI path."""

    URL = "https://ieeexplore.ieee.org/document/11682754/"

    def page(self, pdf=True):
        meta = ['<meta name="citation_title" content="Ieee title">',
                '<meta name="citation_doi" content="10.1109/JSTQE.2026.3731457">']
        if pdf:
            meta.append('<meta name="citation_pdf_url" '
                        'content="https://ieeexplore.ieee.org/stampPDF/getPDF.jsp'
                        '?tp=&arnumber=11682754">')
        return "<html><head>" + "".join(meta) + "</head><body></body></html>"

    def make(self):
        be = ieee.IeeeBackend()
        speed_up(be)
        return be

    def test_a_waf_page_is_a_gate(self):
        self.assertTrue(is_waf_page(WAF_PAGE, 202))
        self.assertTrue(is_waf_page("", 202))
        self.assertFalse(is_waf_page("<html>article</html>", 200))

    def test_the_traffic_shield_interstitial_is_a_gate(self):
        # F5 serves it with a 200 and the item title in it; parsing it as the item used to look
        # like a bare "not entitled" refusal instead of the gate it is.
        self.assertTrue(is_waf_page(TSPD_PAGE, 200))
        be = self.make()
        stub(be, (200, TSPD_PAGE))
        with self.assertRaises(BotWall) as ctx:
            be.get(self.URL)
        self.assertIn("WAF", str(ctx.exception))

    def test_a_sign_in_page_is_not_accepted_as_the_item(self):
        # Xplore redirects an unentitled session to /Xplore/login.jsp with a 200.
        be = self.make()
        stub(be, (302, "", {"location": "https://ieeexplore.ieee.org/Xplore/login.jsp"}),
             (200, "<html><head><title>Sign In to IEEE Xplore</title></head>"
                   "<body>Sign in</body></html>"))
        with self.assertRaises(BotWall) as ctx:
            be.get(self.URL)
        self.assertIn("sign-in page", str(ctx.exception))

    def test_the_nav_wording_alone_is_not_open_access(self):
        # Xplore's nav and footer say "Open Access" on every page, paywalled ones included.
        self.assertFalse(ieee.is_open_access('<nav><a href="/xplore/access">Open Access</a></nav>'))

    def test_the_badge_class_is_open_access(self):
        self.assertTrue(ieee.is_open_access('<span class="document-badge open-access">OA</span>'))
        self.assertTrue(ieee.is_open_access("<span class='open-access'>OA</span>"))

    def test_the_meta_query_tail_survives(self):
        # stampPDF wants ``tag``/``isnumber`` beside ``arnumber``; the meta value is authoritative.
        page = ('<meta name="citation_pdf_url" content="https://ieeexplore.ieee.org/'
                'stampPDF/getPDF.jsp?tp=&arnumber=11682754&tag=1">')
        self.assertEqual(ieee.extract_pdf_url(page, "", "11682754"),
                         "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp"
                         "?tp=&arnumber=11682754&tag=1")

    def test_a_waf_202_becomes_the_waf_message(self):
        be = self.make()
        stub(be, (202, WAF_PAGE))
        with self.assertRaises(BotWall) as ctx:
            be.get(self.URL)
        self.assertIn("WAF challenge", str(ctx.exception))
        self.assertIn("aws-waf-token", str(ctx.exception))

    def test_an_arnumber_url_stays_a_url_then_becomes_the_page_doi(self):
        be = self.make()
        self.assertEqual(ieee.normalize_key(self.URL), f"url:{self.URL}")
        stub(be, (200, self.page()))
        self.assertEqual(be.get(self.URL).key, "doi:10.1109/JSTQE.2026.3731457")

    def test_the_pdf_endpoint_is_taken_from_the_page(self):
        be = self.make()
        stub(be, (200, self.page()))
        self.assertEqual(be.get(self.URL).attachments,
                         ["https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=11682754"])

    def test_the_template_needs_the_arnumber(self):
        # No link on the page: the rewrite must use the landed id, not an empty arnumber.
        be = self.make()
        stub(be, (200, self.page(pdf=False)))
        self.assertIn("arnumber=11682754", be.get(self.URL).attachments[0])

    def test_the_arnumber_is_read_out_of_either_url_shape(self):
        be = self.make()
        self.assertEqual(be.item_id(self.URL), "11682754")
        self.assertEqual(be.item_id("https://ieeexplore.ieee.org/stamp/stamp.jsp"
                                    "?tp=&arnumber=11682754"), "11682754")

    def test_a_login_redirect_is_reported_as_the_entitlement(self):
        be = ieee.IeeeBackend(cookie="aws-waf-token=abc", user_agent="UA/1")
        speed_up(be)
        stub(be, (200, self.page()),
             (302, "", {"location": "https://ieeexplore.ieee.org/Xplore/login.jsp?authDecision=-203"}),
             (200, "<html>login</html>"))
        with self.assertRaises(Paywalled) as ctx:
            be.get_attachment(self.URL)
        self.assertIn("login page", str(ctx.exception))

    def test_a_promoted_url_key_shares_the_memo(self):
        be = self.make()
        pdf = ("https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=11682754")
        rec = stub(be, (200, self.page()), (200, b"%PDF-1.7"))
        be.get(self.URL)
        be.get_attachment("doi:10.1109/JSTQE.2026.3731457")
        self.assertEqual(rec.urls, [self.URL, pdf])


class SiteHostTest(unittest.TestCase):
    """Every entitled reader recognises its own host (an OA link found elsewhere may need it)."""

    def test_each_backend_claims_its_own_hosts_only(self):
        from litcli.backends import sciencedirect, springer, wiley

        cases = ((sciencedirect.ScienceDirectBackend, "https://www.sciencedirect.com/a",
                  "https://evil.example.org/a"),
                 (springer.SpringerBackend, "https://link.springer.com/article/10.1007/x",
                  "https://evil.example.org/a"),
                 (wiley.WileyBackend, "https://onlinelibrary.wiley.com/doi/10.1002/x",
                  "https://evil.example.org/a"))
        for cls, own, other in cases:
            with self.subTest(cls.__name__):
                backend = cls()
                self.assertTrue(backend.matches_url(own))
                self.assertFalse(backend.matches_url(other))


class OwnWordingTest(unittest.TestCase):
    """The two migrated sites keep their own gate wording instead of the generic one."""

    def test_aip_keeps_its_own_gate_wording(self):
        be = aip.AipBackend()
        speed_up(be)
        stub(be, (403, BLOCK_PAGE))
        with self.assertRaises(BotWall) as ctx:
            be.get("10.1063/1.870872")
        self.assertIn("AIP Publishing", str(ctx.exception))

    def test_tandf_names_itself_and_its_cookie(self):
        be = tandf.TandfBackend()
        speed_up(be)
        stub(be, (403, BLOCK_PAGE))
        with self.assertRaises(BotWall) as ctx:
            be.get("10.1080/17460441.2026.2762809")
        self.assertIn("Taylor & Francis", str(ctx.exception))
        self.assertIn("[backends.tandf]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
