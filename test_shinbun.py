#!/usr/bin/env python3
"""shinbun test suite — fully offline.

Every fetch is exercised against canned fixtures through the injectable
_get seam (or a patched shinbun.http_get); the only sockets ever opened
are loopback, in the SOCKS5 wire-format test, which the spec requires.
A test that touches the real network is a regression.
"""

import contextlib
import gzip
import io
import os
import socket
import threading
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import shinbun

# A fixed terminal width keeps the col-80 assertions meaningful anywhere.
os.environ["COLUMNS"] = "80"
os.environ["SHINBUN_NO_JITTER"] = "1"

DAY = "2026-09-02"

# ------------------------------------------------------------------ fixtures

NPR_INDEX_HTML = """<html><body><ul>
<li><a href="/nx-s1-1" class="topic-title">Stub &amp; Headline</a></li>
<li><a  class="topic-title"   href='/nx-s1-2'>Second Headline</a></li>
</ul></body></html>"""

HN_RSS = """<rss><channel><title>Hacker News: Front Page</title>
<link>https://news.ycombinator.com/</link>
<item><title><![CDATA[Stub HN One]]></title><pubDate>d</pubDate>
<link>https://example.com/one</link></item>
<item><title><![CDATA[Stub HN Two & more]]></title><pubDate>d</pubDate>
<link>https://example.com/two</link></item>
</channel></rss>"""

AJ_RSS = """<rss><channel><title>Al Jazeera</title>
<item><title>World Stub One</title><link>https://aje.example/one</link>
<pubDate>d</pubDate></item>
<item><title>World Stub Two</title><link>https://aje.example/two</link>
<pubDate>d</pubDate></item>
</channel></rss>"""

DW_RSS = """<rss><channel><title>World | Deutsche Welle</title>
<item><title>DW Stub One</title><link>https://dw.example/one</link>
<pubDate>d</pubDate></item>
</channel></rss>"""

UN_RSS = """<rss><channel><title>UN News</title>
<item><title>UN Stub One</title><link>https://un.example/one</link>
<pubDate>d</pubDate></item>
</channel></rss>"""

# The portal's daily-subpage heading ships NON-BREAKING spaces (&nbsp;),
# exactly as the real portal emits them. Subsection heads are
# <p><b>Name</b></p> (NOT list items) and the blurbs under them nest
# topic/sub-topic <ul>s three deep — the real portal's shape.
PORTAL_HTML = """<html><head><title>skin junk</title>
<style>.junk{color:red}</style></head><body>
<p>Maintenance banner junk above the news</p>
<h2>Topics in the news</h2>
<ul><li>edit</li><li>history</li><li>watch</li></ul>
<h3>September&nbsp;2,&nbsp;2026&nbsp;(2026-09-02) (Wednesday)</h3>
<p><b>Conflicts and attacks</b></p>
<ul><li>A war
<ul><li>A front
<ul><li>A thing happened in a place with some details attached.</li>
<li>Another thing happened elsewhere entirely.</li></ul></li></ul></li></ul>
<ul><li>Nominate an article</li></ul>
<p>More September 2026 events...</p>
<p>Ongoing events reference junk that must be cut</p>
<script>var junk = 1;</script>
</body></html>"""

ARTICLE_HTML = """<html><body>
<p>Text-Only Version Go To Full Site</p>
<p>NPR &gt; Economy</p>
<p>Article body line</p>
<p>Topics</p>
<p>News Culture Music</p>
<p>footer junk</p>
</body></html>"""

ARTICLE_URL = "https://text.npr.org/nx-s1-12345"

FULL_MAP = {
    "https://text.npr.org/": NPR_INDEX_HTML,
    "https://hnrss.org/frontpage?points=100": HN_RSS,
    "https://www.aljazeera.com/xml/rss/all.xml": AJ_RSS,
    "https://rss.dw.com/xml/rss-en-world": DW_RSS,
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml": UN_RSS,
    shinbun.PORTAL_URL: PORTAL_HTML,
    ARTICLE_URL: ARTICLE_HTML,
}


def fake_getter(mapping, fail_match=None, log=None):
    """A canned http_get: fail_match substrings raise, like a dead source."""
    def get(url):
        if log is not None:
            log.append(url)
        if fail_match and fail_match in url:
            raise shinbun.FetchError(f"canned failure for {url}")
        if url in mapping:
            return mapping[url]
        raise shinbun.FetchError(f"no fixture for {url}")
    return get


def run_main(argv):
    """main() with stdout/stderr captured; returns (exit, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = shinbun.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class ShinbunTestCase(unittest.TestCase):
    """Redirects DATA_DIR into a TemporaryDirectory and resets every piece
    of module state main() touches."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = {name: getattr(shinbun, name) for name in
                       ("DATA_DIR", "WIDTH", "COLOR", "STRICT", "ROUTE",
                        "FELL_BACK")}
        shinbun.DATA_DIR = Path(self.tmp.name) / "shinbun"
        shinbun.WIDTH = 80
        shinbun.COLOR = False
        shinbun.STRICT = False
        shinbun.ROUTE = "undecided"
        shinbun.FELL_BACK = False
        # most tests want decided-tor state without opening a socket
        shinbun.ROUTE = "tor"
        self._sleep = mock.patch.object(shinbun, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(shinbun, name, value)

    def archive(self, argv_extra=(), mapping=FULL_MAP, fail_match=None,
                log=None):
        get = fake_getter(mapping, fail_match, log)
        with mock.patch.object(shinbun, "http_get", get):
            return run_main(["--archive", DAY, "--no-color",
                             *argv_extra])

    def edition_path(self, day=DAY):
        return shinbun.DATA_DIR / f"{day}.txt"

    def edition(self, day=DAY):
        return self.edition_path(day).read_text()


# ---------------------------------------------------------------- the edition


class TestEdition(ShinbunTestCase):

    def test_archive_writes_all_sections(self):
        code, out, err = self.archive()
        self.assertEqual(code, 0, err)
        text = self.edition()
        for marker in ("DAILY EDITION", " NPR ─", " WORLD ─",
                       " DEUTSCHE WELLE ─", " UN NEWS ─",
                       " HACKER NEWS ─", " CURRENT EVENTS ─"):
            self.assertIn(marker, text)
        # the canned items made it through the RSS path, not just headers
        self.assertIn("DW Stub One", text)
        self.assertIn("UN Stub One", text)

    def test_section_order(self):
        self.archive()
        text = self.edition()
        order = [text.index(f" {label} ─") for label in
                 ("NPR", "WORLD", "DEUTSCHE WELLE", "UN NEWS",
                  "HACKER NEWS", "CURRENT EVENTS")]
        self.assertEqual(order, sorted(order))

    def test_entity_unescape(self):
        self.archive()
        self.assertIn("Stub & Headline", self.edition())

    def test_npr_code_right_locked_to_col_80(self):
        # col-80 right-locking by CHARACTER count (› is multibyte)
        self.archive()
        line = next(l for l in self.edition().splitlines()
                    if "› nx-s1-1" in l)
        self.assertEqual(shinbun.visible_len(line), 80)
        self.assertTrue(line.startswith(" "))
        self.assertTrue(line.endswith("› nx-s1-1"))

    def test_hn_url_right_locked(self):
        self.archive()
        line = next(l for l in self.edition().splitlines()
                    if "https://example.com/two" in l)
        self.assertEqual(shinbun.visible_len(line), 80)

    def test_long_url_runs_long_unclipped(self):
        # a bare right-locked URL longer than WIDTH is exempt from clip():
        # a clipped link can be neither opened nor grepped (news-now parity)
        long_url = "https://example.com/" + "a" * 90
        mapping = dict(FULL_MAP)
        mapping["https://hnrss.org/frontpage?points=100"] = \
            HN_RSS.replace("https://example.com/two", long_url)
        code, out, err = self.archive(mapping=mapping)
        self.assertEqual(code, 0, err)
        line = next(l for l in self.edition().splitlines()
                    if "example.com/aaa" in l)
        self.assertTrue(line.endswith(long_url))
        self.assertGreater(shinbun.visible_len(line), 80)

    def test_fetch_order_shuffled_and_gaps_widened(self):
        # fetch order is a secrets-shuffled permutation of the six sources
        # (edition RENDER order stays fixed — test_section_order), and the
        # inter-request gaps are FETCH_GAP_MIN + secrets.randbelow(range)
        log = []
        with mock.patch.object(shinbun.secrets, "randbelow",
                               return_value=0) as rb:
            code, out, err = self.archive(log=log)
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(log), sorted(
            ["https://text.npr.org/",
             "https://hnrss.org/frontpage?points=100",
             "https://www.aljazeera.com/xml/rss/all.xml",
             "https://rss.dw.com/xml/rss-en-world",
             "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
             shinbun.PORTAL_URL]))
        # randbelow drives BOTH the shuffle and the gaps
        self.assertIn(120 - 5 + 1, [c.args[0] for c in rb.call_args_list])
        # five gaps (6 sources), each FETCH_GAP_MIN + 0 with randbelow at 0
        gaps = [c.args[0] for c in self._sleep.call_args_list]
        self.assertEqual(gaps, [shinbun.FETCH_GAP_MIN] * 5)
        # Fisher-Yates with j=0 always is a fixed rotation — proves the
        # wire order is actually shuffled, not SOURCES order
        self.assertNotEqual(log, ["https://text.npr.org/",
                                  "https://hnrss.org/frontpage?points=100",
                                  "https://www.aljazeera.com/xml/rss/all.xml",
                                  "https://rss.dw.com/xml/rss-en-world",
                                  "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
                                  shinbun.PORTAL_URL])

    def test_hn_items_numbered(self):
        self.archive()
        self.assertIn(" 1. Stub HN One", self.edition())

    def test_rss_items_visually_separated(self):
        # headline, right-locked URL under it, BLANK line before the next
        # item — the NPR layout; without the gap the URL/headline pairs of
        # adjacent items blur together
        self.archive()
        lines = self.edition().splitlines()
        i = next(i for i, l in enumerate(lines) if " 1. Stub HN One" in l)
        self.assertIn("example.com/one", lines[i + 1])
        self.assertEqual(lines[i + 2], "")
        self.assertIn(" 2. Stub HN Two", lines[i + 3])

    def test_rss_long_title_wraps_with_hanging_indent(self):
        long_title = "A title that runs well past eighty columns " * 3
        mapping = dict(FULL_MAP)
        mapping["https://hnrss.org/frontpage?points=100"] = \
            HN_RSS.replace("Stub HN One", long_title.strip())
        code, out, err = self.archive(mapping=mapping)
        self.assertEqual(code, 0, err)
        lines = self.edition().splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith(" 1. A title"))
        self.assertTrue(lines[i + 1].startswith("    "))  # hangs past " 1. "
        for l in (lines[i], lines[i + 1]):
            self.assertLessEqual(shinbun.visible_len(l), 80, l)

    def test_day_rule_wrapping(self):
        # NBSP date heading -> yellow-ruled separator
        self.archive()
        self.assertIn("── September 2, 2026 (Wednesday) ──",
                      self.edition())

    def test_portal_trims_applied(self):
        self.archive()
        text = self.edition()
        self.assertIn("A thing happened in a place", text)
        self.assertNotIn("Maintenance banner junk", text)
        self.assertNotIn("reference junk", text)
        self.assertNotIn("Nominate an article", text)
        self.assertNotIn("var junk", text)

    def test_portal_subcategories_render_as_breadcrumbs(self):
        # subsection heads (<p><b>Name</b></p> in the real markup) render as
        # unbulleted head lines; an li carrying a nested ul is a CATEGORY
        # node (never news — the real chains run four deep before any
        # blurb), so its labels collapse to one "  A › B" breadcrumb with
        # the leaf blurbs bulleted under it, no blank lines inside a group
        self.archive()
        text = self.edition()
        self.assertIn("\nConflicts and attacks\n", text)
        self.assertNotIn("- Conflicts and attacks", text)
        self.assertIn("\n  A war › A front\n", text)
        self.assertNotIn("- A war", text)
        self.assertIn("\n  - A thing happened in a place", text)
        # same-group blurbs stay together — no blank line between them
        self.assertIn("attached.\n  - Another thing happened", text)
        self.assertNotIn("\n    -", text)

    def test_every_line_fits_width(self):
        self.archive()
        for line in self.edition().splitlines():
            self.assertLessEqual(shinbun.visible_len(line), 80, line)

    def test_masthead_says_tor(self):
        self.archive()
        self.assertRegex(self.edition(),
                         r"THE DAILY EDITION — 2026-09-02 · pulled "
                         r"\d\d:\d\d via tor")

    def test_one_dead_source_fails_soft(self):
        code, out, err = self.archive(fail_match="hnrss")
        self.assertEqual(code, 0)
        text = self.edition()
        self.assertEqual(text.count("unavailable (fetch failed"), 1)
        self.assertIn("Stub & Headline", text)

    def test_all_dead_never_clobbers(self):
        self.edition_path().parent.mkdir(parents=True, exist_ok=True)
        self.edition_path().write_text("good\n")
        code, out, err = self.archive(fail_match="http")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.edition_path().read_text(), "good\n")

    def test_bad_date_rejected(self):
        code, out, err = run_main(["--archive", "not-a-date"])
        self.assertNotEqual(code, 0)

    def test_debug_reports_source_error(self):
        code, out, err = self.archive(argv_extra=["--debug"],
                                      fail_match="hnrss")
        self.assertIn("hn:", err)
        self.assertIn("FetchError", err)


# ------------------------------------------------------------------ read mode


class TestReadMode(ShinbunTestCase):

    def test_read_pages_todays_edition_without_network(self):
        self.archive()
        with mock.patch.object(shinbun, "http_get") as get:
            code, out, err = run_main(["--no-pager", "--no-color"])
        self.assertEqual(code, 0)
        get.assert_not_called()
        self.assertIn("DAILY EDITION", out)

    def test_read_falls_back_to_newest_edition(self):
        self.archive()
        self.edition_path().rename(self.edition_path("1999-01-01"))
        with mock.patch.object(shinbun, "http_get") as get:
            code, out, err = run_main(["--no-pager"])
        self.assertEqual(code, 0)
        get.assert_not_called()
        self.assertIn("DAILY EDITION", out)

    def test_read_empty_dir_is_a_loud_hint(self):
        with mock.patch.object(shinbun, "http_get") as get:
            code, out, err = run_main(["--no-pager"])
        self.assertNotEqual(code, 0)
        get.assert_not_called()
        self.assertIn("shinbun --archive", err)


# -------------------------------------------------------------- --read <code>


class TestReadArticle(ShinbunTestCase):

    def article_path(self, code="nx-s1-12345"):
        return shinbun.DATA_DIR / "articles" / f"{code}.txt"

    def test_read_pulls_strips_archives_and_pages(self):
        log = []
        get = fake_getter(FULL_MAP, log=log)
        with mock.patch.object(shinbun, "http_get", get):
            code, out, err = run_main(["--read", "nx-s1-12345", "--no-color"])
        self.assertEqual(code, 0, err)
        self.assertEqual(log, [ARTICLE_URL])
        text = self.article_path().read_text()
        self.assertIn("Article body line", text)
        self.assertNotIn("Text-Only Version", text)
        self.assertNotIn("footer junk", text)
        # the pull stamp rides as the first line of the archived file
        first = text.splitlines()[0]
        self.assertRegex(first, r"nx-s1-12345 · pulled \d{4}-\d{2}-\d{2} "
                                r"\d{2}:\d{2} via tor")
        self.assertIn("Article body line", out)

    def test_read_accepts_full_url(self):
        log = []
        get = fake_getter(FULL_MAP, log=log)
        with mock.patch.object(shinbun, "http_get", get):
            code, out, err = run_main(
                ["--read", ARTICLE_URL, "--no-color"])
        self.assertEqual(code, 0, err)
        self.assertEqual(log, [ARTICLE_URL])
        self.assertTrue(self.article_path().exists())

    def test_reread_is_local(self):
        get = fake_getter(FULL_MAP)
        with mock.patch.object(shinbun, "http_get", get):
            run_main(["--read", "nx-s1-12345", "--no-color"])
        with mock.patch.object(shinbun, "http_get") as get2:
            code, out, err = run_main(["--read", "nx-s1-12345",
                                       "--no-color"])
        self.assertEqual(code, 0)
        get2.assert_not_called()
        self.assertIn("Article body line", out)
        self.assertIn("via tor", out)

    def test_failed_read_leaves_no_file(self):
        get = fake_getter(FULL_MAP, fail_match="nx-s1-99999")
        with mock.patch.object(shinbun, "http_get", get):
            code, out, err = run_main(["--read", "nx-s1-99999"])
        self.assertNotEqual(code, 0)
        self.assertFalse(self.article_path("nx-s1-99999").exists())

    def test_empty_render_leaves_no_file(self):
        get = fake_getter({ARTICLE_URL: "<html><body></body></html>"})
        with mock.patch.object(shinbun, "http_get", get):
            code, out, err = run_main(["--read", "nx-s1-12345"])
        self.assertNotEqual(code, 0)
        self.assertFalse(self.article_path().exists())


# -------------------------------------------------------------------- parsing


class TestParsing(unittest.TestCase):

    def test_renderer_bullets_and_script_skip(self):
        lines = shinbun.render_html(
            "<p>para one</p><script>x = 1</script>"
            "<ul><li>a bullet item that should get a dash prefix</li></ul>")
        self.assertIn("para one", lines)
        self.assertIn("- a bullet item that should get a dash prefix", lines)
        self.assertFalse(any("x = 1" in l for l in lines))

    def test_renderer_wraps_bullets_with_hang_indent(self):
        lines = shinbun.render_html("<ul><li>" + "word " * 30 + "</li></ul>")
        wrapped = [l for l in lines if l]
        self.assertGreater(len(wrapped), 1)
        self.assertTrue(all(l.startswith("- ") or l.startswith("  ")
                            for l in wrapped))
        self.assertTrue(all(len(l) <= 80 for l in wrapped))

    def test_renderer_nested_lists_indent(self):
        # a header li carrying a nested ul of blurbs — the header must read
        # as a level ABOVE its items
        lines = shinbun.render_html(
            "<ul><li><b>Conflicts and attacks</b><ul>"
            "<li>first blurb</li><li>second blurb</li>"
            "</ul></li><li><b>Disasters</b><ul>"
            "<li>third blurb</li></ul></li></ul>")
        self.assertIn("- Conflicts and attacks", lines)
        self.assertIn("  - first blurb", lines)
        self.assertIn("  - second blurb", lines)
        self.assertIn("- Disasters", lines)
        self.assertIn("  - third blurb", lines)

    def test_renderer_subsections_render_breadcrumb_groups(self):
        # the portal's REAL shape, verified against the live markup: the
        # subsection head is <p><b>Name</b></p> (not a list item); an li
        # carrying a nested ul is a CATEGORY node (chains run four deep
        # before any blurb), only LEAF lis are news. Heads render
        # unbulleted, category labels collapse to one "  A › B"
        # breadcrumb, blurbs bullet under it.
        lines = shinbun.render_html(
            "<p><b>Conflicts and attacks</b></p>"
            "<ul>"
            "<li>War A<ul><li>Front X<ul>"
            "<li>blurb one</li><li>blurb two</li></ul></li></ul></li>"
            "<li>War B<ul><li>blurb three</li></ul></li>"
            "<li>bare blurb</li>"
            "</ul>"
            "<p><b>Disasters</b></p><ul><li>blurb four</li></ul>",
            subsections=True)
        self.assertIn("Conflicts and attacks", lines)
        self.assertNotIn("- Conflicts and attacks", lines)
        self.assertIn("  War A › Front X", lines)
        self.assertIn("  War B", lines)
        self.assertIn("  - blurb one", lines)
        self.assertIn("  - blurb two", lines)
        self.assertIn("  - blurb three", lines)
        self.assertIn("- bare blurb", lines)
        self.assertIn("Disasters", lines)
        self.assertIn("- blurb four", lines)
        # same-group blurbs stay together; a blank line splits groups
        one = lines.index("  - blurb one")
        self.assertEqual(lines[one + 1], "  - blurb two")
        self.assertEqual(lines[one + 2], "")
        self.assertEqual(lines[one + 3], "  War B")
        # a sibling category does NOT inherit the previous chain: blurb
        # three's context is "War B" alone, and a new head drops it all
        self.assertNotIn("  War A › Front X › War B", lines)
        self.assertNotIn("  - blurb four", lines)
        self.assertFalse(any(l.startswith("    -") for l in lines))

    def test_renderer_bold_block_stays_plain_without_subsections(self):
        # the NPR --read path passes no flag: a standalone bold paragraph is
        # prose, not a section head — no bullet, no indentation games
        lines = shinbun.render_html("<p><b>just a bold sentence</b></p>")
        self.assertIn("just a bold sentence", lines)
        self.assertNotIn("- just a bold sentence", lines)

    def test_renderer_partially_bold_block_is_not_a_head(self):
        # bold LINKS inside a blurb must not promote it: only a block whose
        # ENTIRE text is bold is a subsection head
        lines = shinbun.render_html(
            "<p>Some <b>bold</b> words</p>", subsections=True)
        self.assertIn("Some bold words", lines)
        self.assertNotIn("- Some bold words", lines)

    def test_renderer_nested_bullets_wrap_with_deeper_hang(self):
        lines = shinbun.render_html(
            "<ul><li>header<ul><li>" + "word " * 30 + "</li></ul></li></ul>")
        wrapped = [l for l in lines if l and "header" not in l]
        self.assertGreater(len(wrapped), 1)
        self.assertTrue(all(l.startswith("  - ") or l.startswith("    ")
                            for l in wrapped))
        self.assertTrue(all(len(l) <= 80 for l in wrapped))

    def test_rss_limits_and_unwraps_cdata(self):
        items = shinbun.parse_rss(HN_RSS, 20)
        self.assertEqual(items[0], ("Stub HN One", "https://example.com/one"))

    def test_npr_index_parser_is_attribute_order_agnostic(self):
        parser = shinbun.NprIndexParser()
        parser.feed(NPR_INDEX_HTML)
        self.assertEqual(parser.items,
                         [("Stub & Headline", "/nx-s1-1"),
                          ("Second Headline", "/nx-s1-2")])


# --------------------------------------------------------------------- jitter


class TestJitter(ShinbunTestCase):

    def test_jitter_uses_secrets_and_respects_cap(self):
        # randbelow serves: the pre-pull jitter, then the 5 Fisher-Yates
        # shuffle draws, then the 5 inter-fetch gap draws
        with mock.patch.object(shinbun.secrets, "randbelow",
                               side_effect=[42, 0, 0, 0, 0, 0,
                                            2, 2, 2, 2, 2]) as rb:
            code, out, err = self.archive(argv_extra=["--jitter", "10"])
        self.assertEqual(code, 0, err)
        # the pre-pull sleep comes first and is capped at MINUTES*60 + 1
        self.assertEqual(rb.call_args_list[0], mock.call(601))
        self.assertEqual(self._sleep.call_args_list[0], mock.call(42))
        # the shuffle draws (i = 5, 4, 3, 2, 1)
        self.assertEqual([c.args[0] for c in rb.call_args_list[1:6]],
                         [6, 5, 4, 3, 2])
        # inter-fetch gaps: FETCH_GAP_MIN + randbelow(range), per source
        # after the 1st
        gap_range = shinbun.FETCH_GAP_MAX - shinbun.FETCH_GAP_MIN + 1
        self.assertEqual([c.args[0] for c in rb.call_args_list[6:]],
                         [gap_range] * 5)
        self.assertEqual([c.args[0] for c in self._sleep.call_args_list[1:]],
                         [shinbun.FETCH_GAP_MIN + 2] * 5)

    def test_jitter_rejected_without_archive(self):
        for argv in (["--jitter", "5"], ["--read", "x", "--jitter", "5"]):
            code, out, err = run_main(argv)
            self.assertNotEqual(code, 0, argv)


# ------------------------------------------------------------------ tor model


class TestTorModel(ShinbunTestCase):

    def setUp(self):
        super().setUp()
        shinbun.ROUTE = "undecided"

    def _tcp_broken_socks(self, host, port):
        if port == shinbun.SOCKS_PORT:
            raise ConnectionRefusedError("no tor here")
        return "DIRECT-SOCKET"

    def test_tor_down_warns_and_falls_back(self):
        with mock.patch.object(shinbun, "_tcp_connect",
                               side_effect=self._tcp_broken_socks):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                conn = shinbun._open_connection("example.com", 443)
        self.assertEqual(conn, "DIRECT-SOCKET")
        self.assertEqual(shinbun.ROUTE, "clearnet")
        self.assertTrue(shinbun.FELL_BACK)
        self.assertIn("CLEARNET", err.getvalue())

    def test_tor_down_strict_dies(self):
        shinbun.STRICT = True
        with mock.patch.object(shinbun, "_tcp_connect",
                               side_effect=self._tcp_broken_socks):
            with self.assertRaises(SystemExit):
                shinbun._open_connection("example.com", 443)

    def test_no_tor_is_silent(self):
        shinbun.ROUTE = "clearnet"  # what main() sets for --no-tor
        with mock.patch.object(shinbun, "_tcp_connect",
                               return_value="DIRECT-SOCKET") as tcp:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                conn = shinbun._open_connection("example.com", 443)
        self.assertEqual(conn, "DIRECT-SOCKET")
        self.assertFalse(shinbun.FELL_BACK)
        self.assertEqual(err.getvalue(), "")
        # the SOCKS port is never even tried
        tcp.assert_called_once_with("example.com", 443)

    def test_fallback_marks_the_masthead(self):
        # a tor-down archive pull must carry the CLEARNET warning line
        shinbun.FELL_BACK = True
        lines, errors, ok = shinbun.build_edition(
            DAY, datetime(2026, 9, 2, 5, 41), _get=fake_getter(FULL_MAP))
        text = "\n".join(lines)
        self.assertIn("via CLEARNET (tor unreachable)", text)
        self.assertIn("via clearnet", text)


# ------------------------------------------------- socks5 wire format (local)


class FakeSocksServer(threading.Thread):
    """Just enough SOCKS5 to record the handshake and answer one HTTP
    request per accepted connection. Loopback only — no real network."""

    def __init__(self, body=b"hello tor body", extra_headers=b""):
        super().__init__(daemon=True)
        self.body = body
        self.extra_headers = extra_headers
        self.requests = []  # (atyp, host_bytes, port)
        self.http_requests = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                break
            try:
                self.handle(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def handle(self, conn):
        ver, nmethods = conn.recv(1)[0], conn.recv(1)[0]
        conn.recv(nmethods)
        conn.sendall(b"\x05\x00")  # no-auth accepted
        head = self._recv(conn, 4)
        atyp = head[3]
        if atyp == 3:
            n = self._recv(conn, 1)[0]
            host = self._recv(conn, n)
        elif atyp == 1:
            host = self._recv(conn, 4)
        else:
            host = self._recv(conn, 16)
        port = int.from_bytes(self._recv(conn, 2), "big")
        self.requests.append((atyp, host, port))
        conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        self.http_requests.append(req)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                     + self.extra_headers
                     + b"Connection: close\r\n\r\n" + self.body)

    @staticmethod
    def _recv(conn, n):
        buf = b""
        while len(buf) < n:
            buf += conn.recv(n - len(buf))
        return buf

    def close(self):
        self._stop = True
        self.sock.close()


class TestSocksWire(ShinbunTestCase):

    def setUp(self):
        super().setUp()
        shinbun.ROUTE = "undecided"
        self.server = FakeSocksServer()
        self.server.start()
        self.addCleanup(self.server.close)
        self._saved_socks = (shinbun.SOCKS_HOST, shinbun.SOCKS_PORT)
        shinbun.SOCKS_HOST = "127.0.0.1"
        shinbun.SOCKS_PORT = self.server.port
        self.addCleanup(self._restore_socks)

    def _restore_socks(self):
        shinbun.SOCKS_HOST, shinbun.SOCKS_PORT = self._saved_socks

    def test_connect_carries_atyp3_domain_and_get_succeeds(self):
        body = shinbun.http_get("http://example.com/news")
        self.assertEqual(body, "hello tor body")
        self.assertEqual(shinbun.ROUTE, "tor")
        atyp, host, port = self.server.requests[0]
        # ATYP=3 + the ASCII domain name: remote DNS (socks5h semantics)
        self.assertEqual(atyp, 3)
        self.assertEqual(host, b"example.com")
        self.assertEqual(port, 80)
        req = self.server.http_requests[0]
        self.assertIn(b"GET /news HTTP/1.1", req)
        self.assertIn(b"Host: example.com", req)
        self.assertIn(shinbun.UA.encode(), req)

    def test_gzip_content_encoding_decoded(self):
        # news.un.org's CDN forces Content-Encoding: gzip regardless of
        # Accept-Encoding — the client must decode it to get text
        self.server.close()
        self.server = FakeSocksServer(
            body=gzip.compress(b"hello gzipped body"),
            extra_headers=b"Content-Encoding: gzip\r\n")
        self.server.start()
        self.addCleanup(self.server.close)
        shinbun.SOCKS_PORT = self.server.port
        self.assertEqual(shinbun.http_get("http://example.com/news"),
                         "hello gzipped body")


# --------------------------------------------------------------------- chunks


class TestChunked(unittest.TestCase):

    def test_chunked_decoding(self):
        raw = (b"5\r\nhello\r\n6\r\n world\r\n"
               b"A;ext=1\r\n0123456789\r\n0\r\nX-Trailer: x\r\n\r\n")
        self.assertEqual(shinbun._read_chunked(io.BytesIO(raw)),
                         b"hello world0123456789")

    def test_chunked_truncated_raises(self):
        with self.assertRaises(shinbun.FetchError):
            shinbun._read_chunked(io.BytesIO(b"8\r\nshort\r\n"))


if __name__ == "__main__":
    unittest.main()
