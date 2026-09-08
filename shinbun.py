#!/usr/bin/env python3
"""shinbun — the daily newspaper: ONE dated 80-column plaintext edition of
world news, pulled over Tor each morning, read back offline.

Usage:
  shinbun                       page the freshest LOCAL edition (today's file,
                                else the newest in DATA_DIR) — ZERO network
  shinbun --archive [DATE]      pull today's edition (default DATE: today) to
                                $DATA_DIR/DATE.txt, atomically
  shinbun --read <code>         pull ONE NPR article (the › codes in the
                                edition's NPR section: nx-s1-…, g-s1-…),
                                strip it, archive it under
                                $DATA_DIR/articles/, and page it — re-reads
                                of an archived article are LOCAL

Why: the newspaper model instead of an inbox-shaped RSS reader — a jittered
systemd timer (or cron with --jitter) pulls one dated edition each morning,
the read path is a local file + less with ZERO network traffic, and
yesterday's file is the archive. No app state, nothing to learn; grep and
$EDITOR work over the archive dir like any other plaintext. One file, Python
3 standard library only, no subprocess fetchers (the pager is the only child
process ever spawned).

Tor: every fetch goes through the local tor daemon's SOCKS port with socks5h
semantics — ATYP=3, the EXIT node resolves DNS, because a lookup in the local
resolver's log would re-link the session. If the SOCKS port does not answer
on the first fetch of a run, the default is fail-SOFT-WITH-WARNING: a loud
stderr warning plus a "via CLEARNET (tor unreachable)" line under the
masthead, and the run continues direct. --strict fails closed instead; --no-tor
is deliberate clearnet and stays silent. The pulls BLEND IN: the UA is a
stock Firefox ESR string (Tor Browser's own blend value — a custom UA would
fingerprint every fetch), no cookies, no JS, only the text comes back.

Traffic budget: exactly 4 GETs per edition pull (one per source), text only —
subresources and images are never fetched. The pulls are anti-fingerprint
shaped: fetch order is shuffled per run (secrets Fisher-Yates) and between
fetches the pull sleeps a secrets-random FETCH_GAP_MIN..MAX seconds, so the
day's requests are neither a fixed burst nor a fixed sequence; --jitter
sleeps up to N minutes BEFORE pulling, for cron/non-systemd scheduling.

Sources, all keyless and rendered locally:
  NPR             text.npr.org index — headlines, each with its short code
                  right-locked to col 80 (the --read handle).
  WORLD           Al Jazeera RSS — top 15 title/link pairs.
  HACKER NEWS     hnrss.org frontpage RSS — top 20 title/link pairs.
  CURRENT EVENTS  Wikipedia Portal:Current events via action=render (no skin),
                  rendered by the built-in TextRenderer, trimmed to the news,
                  each daily date heading wrapped in a yellow rule.

Format: a NerdFont masthead + section rules in bold ANSI named colors
(16-color, so they track terminal palette themes); bodies stay plain text so
grep over the archive is clean. less needs -R for the colors; the built-in
pager resolution adds -RFX automatically.

SHINBUN_DATA_DIR / SHINBUN_SOCKS / SHINBUN_NO_JITTER override the moving
parts; tests additionally patch http_get() and sleep().
"""

import argparse
import html.parser
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

# ------------------------------------------------------------------ constants

# Tor Browser's blend-in UA (stock Firefox ESR string): a custom UA would
# fingerprint every pull, and Wikimedia 403s curl's default.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
      "Gecko/20100101 Firefox/128.0")

NPR_BASE = "https://text.npr.org"
HN_FEED = "https://hnrss.org/frontpage?points=100"
AJ_FEED = "https://www.aljazeera.com/xml/rss/all.xml"
# action=render returns only the article content HTML; the /wiki/ URL drags
# the whole Wikipedia skin (sidebar, tools, footer) into the render.
PORTAL_URL = ("https://en.wikipedia.org/w/index.php"
              "?title=Portal:Current_events&action=render")

HN_ITEMS = 20
AJ_ITEMS = 15
MAX_REDIRECTS = 5
HTTP_TIMEOUT = 60
# inter-request gap (seconds): a secrets-random 5–120 s between fetches, on
# top of the per-run fetch-order shuffle — the four GETs should read as
# unrelated visits, not one burst. Wide enough to matter, small enough that
# a manual --archive never waits more than ~6 minutes total.
FETCH_GAP_MIN = 5
FETCH_GAP_MAX = 120

SOCKS = os.environ.get("SHINBUN_SOCKS", "127.0.0.1:9050")
SOCKS_HOST, _, _socks_port = SOCKS.rpartition(":")
SOCKS_PORT = int(_socks_port or "9050")

if os.environ.get("SHINBUN_DATA_DIR"):
    DATA_DIR = Path(os.environ["SHINBUN_DATA_DIR"])
else:
    _xdg_data = (os.environ.get("XDG_DATA_HOME")
                 or str(Path.home() / ".local/share"))
    DATA_DIR = Path(_xdg_data) / "shinbun"

# Report width: 80 columns at most, narrowed to the terminal in main() so a
# small tmux pane gets an edition that fits instead of one that wraps.
WIDTH = 80
MIN_WIDTH = 48

# Nerd Font glyphs live in the Private Use Area, which less treats as
# non-printable unless told otherwise (the masthead glyph is one).
PUA_PRINTABLE = "E000-F8FF:p,F0000-FFFFD:p,100000-10FFFD:p"
MASTHEAD_GLYPH = "\uf1ea"  # nf-fa-newspaper_o

# Mutable run state, set by main() and mutated by tests (sdwx precedent):
COLOR = True          # named-16-color bold ANSI in the edition
STRICT = False        # --strict: tor down = die, never clearnet
ROUTE = "undecided"   # "tor" | "clearnet", decided on the first fetch
FELL_BACK = False     # tor was unreachable and the run went clearnet


class FetchError(Exception):
    """Any failure pulling a document; sources fail soft on this."""


def sleep(seconds):
    """The one sleep seam: tests patch this; SHINBUN_NO_JITTER=1 zeros it."""
    if os.environ.get("SHINBUN_NO_JITTER"):
        return
    time.sleep(seconds)


# ----------------------------------------------------------------------- ansi

# Named 16-color bold only — tracks the terminal palette, no hardcoded hex.
ANSI = {
    "reset": "\x1b[0m",
    "masthead": "\x1b[1;34m",
    "rule": "\x1b[1;36m",
    "day": "\x1b[1;33m",
    "warn": "\x1b[1;31m",
    # regular (non-bold) blue: the right-locked URL under a headline —
    # visually quieter than the plain-text headline it belongs to
    "link": "\x1b[34m",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def style(text, name):
    if not COLOR or not name:
        return text
    return f"{ANSI[name]}{text}{ANSI['reset']}"


def visible_len(s):
    return len(ANSI_RE.sub("", s))


def clip(s, width):
    """Hard cap on visible width, ANSI-aware.

    Escapes are copied through without counting toward the width, and reset
    is re-appended only if the line carried escapes. This is the backstop
    that keeps every edition line inside WIDTH at any terminal size;
    renderers wrap and pad first.
    """
    if visible_len(s) <= width:
        return s
    out, n, i = [], 0, 0
    while i < len(s) and n < width:
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            out.append(s[i])
            n += 1
            i += 1
    if "\x1b[" in s:
        out.append(ANSI["reset"])
    return "".join(out)


def rule(label):
    """" NPR " + a box-drawing rule out to WIDTH, in cyan."""
    text = f" {label} "
    return style(text + "─" * max(0, WIDTH - len(text)), "rule")


def right_lock(s):
    """Pad with spaces so the string ENDS at column WIDTH.

    len() counts CHARACTERS where a byte-oriented pad would put a non-ASCII
    glyph like › columns short. Longer than WIDTH (a long URL) runs long —
    clip_line() exempts bare URLs at assembly: a clipped link can be neither
    opened nor grepped (news-now's right80 rule).
    """
    return " " * max(0, WIDTH - visible_len(s)) + s


def clip_line(s):
    """clip() with one exemption: a bare right-locked URL line runs long.
    The check strips ANSI first — a styled ("link"-blue) URL line starts
    with the escape, not the scheme."""
    return s if ANSI_RE.sub("", s).lstrip().startswith("http") \
        else clip(s, WIDTH)


# -------------------------------------------------------------- http over tor


def _tcp_connect(host, port):
    return socket.create_connection((host, port), timeout=HTTP_TIMEOUT)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise FetchError("SOCKS5: connection closed mid-handshake")
        buf += chunk
    return buf


def _socks5_handshake(sock, host, port):
    """Handshake a CONNECT through the SOCKS5 port.

    ATYP=3 sends the domain name itself (socks5h semantics): the Tor EXIT
    node resolves DNS, so nothing ever lands in the local resolver's log.
    """
    sock.sendall(b"\x05\x01\x00")  # ver 5, one method: no-auth
    if _recv_exact(sock, 2) != b"\x05\x00":
        raise FetchError("SOCKS5: no-auth method refused")
    host_b = host.encode("idna")
    if len(host_b) > 255:
        raise FetchError(f"SOCKS5: hostname too long: {host!r}")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                 + struct.pack(">H", port))
    head = _recv_exact(sock, 4)
    if head[0] != 5 or head[1] != 0:
        raise FetchError(f"SOCKS5: CONNECT to {host}:{port} failed "
                         f"(reply code {head[1]})")
    # Drain the bound address the proxy reports before the stream starts.
    atyp = head[3]
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif atyp == 4:
        _recv_exact(sock, 16)
    _recv_exact(sock, 2)


def _open_connection(host, port):
    """Open a TCP connection to host:port, over Tor by default.

    The tor-vs-clearnet decision is made ONCE per run, on the first fetch:
    if the SOCKS port itself does not answer (ConnectionRefusedError /
    timeout / OSError from the TCP connect), the run falls back to direct
    connections with a loud warning unless --strict, in which case it dies.
    A failed SOCKS handshake (tor is up but the exit cannot reach the host)
    is a per-host failure, not a reason to abandon tor.
    """
    global ROUTE, FELL_BACK
    if ROUTE == "clearnet":
        return _tcp_connect(host, port)
    try:
        sock = _tcp_connect(SOCKS_HOST, SOCKS_PORT)
    except OSError as exc:
        if STRICT:
            sys.exit(f"shinbun: tor SOCKS {SOCKS_HOST}:{SOCKS_PORT} "
                     f"unreachable ({exc}) — --strict, failing closed")
        ROUTE = "clearnet"
        FELL_BACK = True
        print(f"shinbun: WARNING — tor SOCKS {SOCKS_HOST}:{SOCKS_PORT} "
              f"unreachable ({exc}); pulling over CLEARNET. "
              f"Use --strict to fail closed instead.", file=sys.stderr)
        return _tcp_connect(host, port)
    try:
        _socks5_handshake(sock, host, port)
    except Exception:
        sock.close()
        raise
    ROUTE = "tor"
    return sock


def _read_chunked(f):
    """Decode a Transfer-Encoding: chunked body from a binary file object."""
    out = bytearray()
    while True:
        size_line = f.readline().strip()
        try:
            size = int(size_line.split(b";")[0], 16)  # split off extensions
        except ValueError:
            raise FetchError(f"bad chunk size line: {size_line!r}")
        if size == 0:
            # trailers, if any, run to a blank line
            while f.readline() not in (b"\r\n", b"\n", b""):
                pass
            break
        chunk = f.read(size)  # buffered reads return size bytes unless EOF
        if len(chunk) != size:
            raise FetchError("chunked body truncated")
        out += chunk
        f.read(2)  # the CRLF after each chunk
    return bytes(out)


def http_get(url, redirects=MAX_REDIRECTS):
    """THE one choke point for every byte the program fetches.

    Minimal HTTP/1.1 over the tor SOCKS port (or direct, per the decided
    ROUTE): identity encoding only, Connection: close, the blend-in UA, ≤5
    redirects, body decoded as UTF-8 with replacement. Subresources and
    images are never fetched — the traffic budget is 4 GETs per edition.
    """
    parts = urlsplit(url)
    scheme = parts.scheme or "https"
    host = parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    sock = _open_connection(host, port)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        request = (f"GET {path} HTTP/1.1\r\n"
                   f"Host: {host}\r\n"
                   f"User-Agent: {UA}\r\n"
                   f"Accept: */*\r\n"
                   f"Connection: close\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        f = sock.makefile("rb")
        status_line = f.readline().decode("iso-8859-1").strip()
        m = re.match(r"HTTP/\d(?:\.\d)? (\d{3})", status_line)
        if not m:
            raise FetchError(f"bad status line: {status_line!r}")
        status = int(m.group(1))
        headers = {}
        while True:
            line = f.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("iso-8859-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        if status in (301, 302, 303, 307, 308) and "location" in headers:
            if redirects <= 0:
                raise FetchError(f"too many redirects (from {url})")
            location = urljoin(url, headers["location"])
            f.close()
            sock.close()
            sock = None
            return http_get(location, redirects - 1)
        if status >= 400:
            raise FetchError(f"HTTP {status} for {url}")
        if "chunked" in headers.get("transfer-encoding", "").lower():
            body = _read_chunked(f)
        else:
            body = f.read()  # Connection: close — read to EOF
    finally:
        if sock is not None:
            sock.close()
    return body.decode("utf-8", "replace")


# ---------------------------------------------------------------- html → text

# Tags that end the current block and start a new line.
BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "td", "th", "blockquote", "section",
    "article", "header", "footer", "hr", "dl", "dt", "dd", "pre", "figure",
    "figcaption",
}
# Code and metadata, not prose: dropped with their contents.
SKIP_TAGS = {"script", "style", "head", "noscript", "template", "svg"}


class TextRenderer(html.parser.HTMLParser):
    """A poor man's `w3m -dump`, built on html.parser.

    Block-level tags break lines, <li> gets a "- " bullet with a hanging
    indent, script/style content is skipped, runs of ASCII whitespace
    collapse (NBSP is deliberately preserved — the portal's date headings
    use it, and day_separators() normalizes it at match time), and each
    block is wrapped to WIDTH. convert_charrefs delivers entities already
    unescaped.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []  # (is_bullet, text)
        self.buf = []
        self.bullet = False
        self.skip = 0

    def _flush(self):
        text = re.sub(r"[ \t\r\n\f\v]+", " ", "".join(self.buf)).strip()
        if text:
            self.blocks.append((self.bullet, text))
        self.buf = []
        self.bullet = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "li":
            self.bullet = True

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def lines(self, width=None):
        self._flush()
        width = width or WIDTH
        out = []
        for bullet, text in self.blocks:
            if bullet:
                out.extend(textwrap.wrap(text, width, initial_indent="- ",
                                         subsequent_indent="  ") or ["-"])
            else:
                out.extend(textwrap.wrap(text, width) or [""])
            out.append("")  # blank line between blocks, like w3m -dump
        while out and not out[-1]:
            out.pop()
        return out


def render_html(html_text):
    """HTML string → wrapped plaintext lines."""
    renderer = TextRenderer()
    renderer.feed(html_text)
    return renderer.lines()


class NprIndexParser(html.parser.HTMLParser):
    """Extract <a class="topic-title" href="...">text</a> rows from the NPR
    text-only index. Attribute order and whitespace agnostic; entities
    arrive unescaped via convert_charrefs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []  # (title, href)
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        d = dict(attrs)
        if "topic-title" in (d.get("class") or "").split() and d.get("href"):
            self._href = d["href"]
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            title = " ".join("".join(self._buf).split())
            if title:
                self.items.append((title, self._href))
            self._href = None


def parse_rss(xml_text, limit):
    """RSS → [(title, link)], shared by the HN and Al Jazeera feeds.

    ElementTree transparently unescapes entities and unwraps CDATA titles.
    """
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            items.append((title, link))
        if len(items) >= limit:
            break
    return items


# ------------------------------------------------------- portal trims (wiki)

# Start at the "Topics in the news" box (above it is the maintenance banner,
# intro box, and portal link farm); stop at the first "More <Month> <year>
# events..." link or the "Ongoing events" reference header — from there on
# it's the month calendar, the portal footer, and hundreds of lines of
# deaths/conflicts reference tables. Each trim is fail-soft on its own:
# marker gone -> that side passes through.
PORTAL_END_RE = re.compile(r"^(More .* events\.\.\.|Ongoing events)$")


def trim_portal(lines):
    start = next((i for i, l in enumerate(lines)
                  if l.strip() == "Topics in the news"), 0)
    lines = lines[start:]
    end = next((i for i, l in enumerate(lines)
                if PORTAL_END_RE.match(l.strip())), len(lines))
    return lines[:end]


# Daily subpages lead with an edit/history/watch stub (and they render
# embedded in the portal too), and the portal's boxes end with a "Nominate
# an article" link. Both bullet flavors are stripped: "•" (w3m) and "-"
# (TextRenderer's own li rendering).
FURNITURE = {"edit", "history", "watch", "Nominate an article"}


def strip_furniture(lines):
    out = []
    for line in lines:
        text = line.strip()
        for prefix in ("• ", "- "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        if text not in FURNITURE:
            out.append(line)
    return out


# The portal heads each daily subpage with
# "September 2, 2026 (2026-09-02) (Wednesday)" — with NON-BREAKING spaces
# (it writes &nbsp;), so normalize before matching. The portal spans today
# AND tomorrow (timezone overlap), so wrap each date heading in a yellow
# rule: the day boundaries stay visible and adjacent days can't blur
# together. Fail-soft: no date headings -> unchanged.
DAY_HEADING_RE = re.compile(r"^([A-Z][a-z]+) (\d{1,2}), (\d{4}) "
                            r"\(\d{4}-\d{2}-\d{2}\) \(([A-Za-z]+)\)$")


def day_separators(lines):
    out = []
    for line in lines:
        m = DAY_HEADING_RE.match(line.replace("\u00a0", " ").strip())
        if m:
            month, day, year, weekday = m.groups()
            out.append("")
            out.append(style(f" ── {month} {day}, {year} ({weekday}) ──",
                             "day"))
        else:
            out.append(line)
    return out


def trim_article(lines):
    """Strip NPR's text-only page furniture. Both trims are fail-soft:
    marker gone -> that side passes through."""
    lines = list(lines)
    # the "Text-Only Version / Go To Full Site" header line
    for i, line in enumerate(lines):
        if line.strip():
            if line.strip().startswith("Text-Only Version"):
                del lines[i]
            break
    # everything from a bare "Topics" line down is footer nav
    for i, line in enumerate(lines):
        if line.strip() == "Topics":
            lines = lines[:i]
            break
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


# -------------------------------------------------------------------- sources
# One (key, label, function) per section; adding a source is a row here plus
# a src_* function above main(). Each fails soft independently; the edition
# is written unless ALL fail.

def src_npr(_get=None):
    """NPR text-only index: headline, then its short code (the --read
    handle) right-locked to col 80, a blank line between items."""
    doc = (_get or http_get)(NPR_BASE + "/")
    parser = NprIndexParser()
    parser.feed(doc)
    if not parser.items:
        raise FetchError("no headlines parsed from the NPR index")
    lines = []
    for title, href in parser.items:
        code = href.rstrip("/").split("/")[-1]
        # some headlines run long — wrap rather than truncate
        lines.extend(textwrap.wrap(title, WIDTH) or [title])
        lines.append(right_lock(f"› {code}"))
        lines.append("")
    lines.pop()
    return lines


def rss_section(url, limit, _get=None):
    """Numbered title/link pairs, the NPR layout: long titles wrap with a
    hanging indent past the number, the URL right-locked in quiet blue under
    each, a blank line between items. No scores, no comment counts:
    headlines, like everything else."""
    doc = (_get or http_get)(url)
    items = parse_rss(doc, limit)
    if not items:
        raise FetchError(f"no items parsed from {url}")
    lines = []
    for n, (title, link) in enumerate(items, 1):
        head = f"{n:2d}. "
        lines.extend(textwrap.wrap(title, WIDTH, initial_indent=head,
                                   subsequent_indent=" " * len(head))
                     or [head.rstrip()])
        lines.append(style(right_lock(link), "link"))
        lines.append("")
    lines.pop()
    return lines


def src_world(_get=None):
    return rss_section(AJ_FEED, AJ_ITEMS, _get)


def src_hn(_get=None):
    return rss_section(HN_FEED, HN_ITEMS, _get)


def src_events(_get=None):
    doc = (_get or http_get)(PORTAL_URL)
    lines = render_html(doc)
    lines = trim_portal(lines)
    lines = strip_furniture(lines)
    lines = day_separators(lines)
    if not any(l.strip() for l in lines):
        raise FetchError("empty portal render")
    return lines


SOURCES = [
    ("npr", "NPR", src_npr),
    ("world", "WORLD", src_world),
    ("hn", "HACKER NEWS", src_hn),
    ("events", "CURRENT EVENTS", src_events),
]


# -------------------------------------------------------------------- edition


def collapse_blanks(lines):
    """Runs of blank lines collapse to one — furniture-stripping and empty
    portal boxes otherwise leave holes in the page."""
    out = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return out


def shuffled(seq):
    """Fisher-Yates with secrets — random.shuffle is not a CSPRNG, and the
    fetch order is exactly the kind of metadata that should not be
    reproducible from a guessable PRNG state."""
    items = list(seq)
    for i in range(len(items) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        items[i], items[j] = items[j], items[i]
    return items


def via_word():
    return "tor" if ROUTE == "tor" else "clearnet"


def build_edition(day, now, _get=None):
    """Pull every source and assemble the edition.

    Returns (lines, errors, ok_count). The four GETs are anti-fingerprint
    shaped: fetch ORDER is shuffled per run (secrets Fisher-Yates — the
    edition still renders in fixed SOURCES order; only the wire order is
    random) and between fetches the pull sleeps a secrets-random
    FETCH_GAP_MIN..FETCH_GAP_MAX seconds, so the day's requests are neither
    a fixed burst nor a fixed sequence. The masthead is composed AFTER the
    fetches so it can tell the truth about the route taken.
    """
    errors = {}
    bodies = {}
    ok = 0
    for i, (key, label, fn) in enumerate(shuffled(SOURCES)):
        if i:
            sleep(FETCH_GAP_MIN + secrets.randbelow(
                FETCH_GAP_MAX - FETCH_GAP_MIN + 1))
        try:
            lines = fn(_get=_get)
            ok += 1
        except Exception as exc:
            errors[key] = exc
            lines = ["  unavailable (fetch failed — tor down?)"]
        bodies[label] = lines
    out = [style(f"  {MASTHEAD_GLYPH} THE DAILY EDITION — {day} · pulled "
                 f"{now.strftime('%H:%M')} via {via_word()}", "masthead")]
    if FELL_BACK:
        out.append(style("  [!] via CLEARNET (tor unreachable) — this "
                         "edition was pulled in the clear", "warn"))
    out.append("")
    for key, label, fn in SOURCES:
        out.append(rule(label))
        out.extend(bodies[label])
        out.append("")
    return [clip_line(l) for l in collapse_blanks(out)], errors, ok


def write_atomic(path, text):
    """Temp file beside the target + os.replace, so a concurrent reader
    never sees a torn edition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------- pager


def pager_command(env):
    """Return (argv, child_env) for the pager.

    less needs -R to pass ANSI through, -F to skip paging output that
    already fits, and -X to leave the edition in the scrollback. Those go
    on the argv rather than $LESS so they win over the user's env without
    clobbering the rest of it. less also treats Private Use Area codepoints
    (where the Nerd Font masthead glyph lives) as control characters and
    prints "<U+F1EA>", so declare the PUA printable unless the user already
    has an opinion. Any other $PAGER is run as written.
    """
    argv = shlex.split(env.get("PAGER") or "less")
    child = dict(env)
    if argv and os.path.basename(argv[0]) == "less":
        argv.append("-RFX")
        child.setdefault("LESSUTFCHARDEF", PUA_PRINTABLE)
    return argv, child


def emit(text, use_pager, env=None):
    """Write text, through a pager when asked. Degrades to plain stdout if
    the pager is missing, and stays quiet if the reader quits early."""
    if not use_pager:
        print(text)
        return
    argv, child_env = pager_command(os.environ if env is None else env)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, env=child_env,
                                universal_newlines=True)
    except (OSError, ValueError):
        print(text)
        return
    try:
        proc.communicate(text + "\n")
    except (BrokenPipeError, KeyboardInterrupt):
        proc.wait()


# ---------------------------------------------------------------------- modes


def mode_read(use_pager):
    """Page the freshest LOCAL edition. Zero network on this path."""
    path = DATA_DIR / f"{date.today().isoformat()}.txt"
    if not path.exists():
        # filenames sort by date; the newest on disk is the fallback
        editions = sorted(p for p in DATA_DIR.glob("*.txt") if p.is_file())
        if not editions:
            print(f"shinbun: no editions in {DATA_DIR} yet — "
                  f"run: shinbun --archive", file=sys.stderr)
            return 1
        path = editions[-1]
    emit(path.read_text().rstrip("\n"), use_pager)
    return 0


def mode_archive(day, jitter_minutes, debug):
    """Pull the edition to $DATA_DIR/DATE.txt. If every section failed, do
    NOT write (an all-dead pull must not clobber a good file)."""
    if jitter_minutes:
        # cron scheduling without systemd's RandomizedDelaySec: sleep a
        # secrets-random 0..N minutes BEFORE pulling
        sleep(secrets.randbelow(jitter_minutes * 60 + 1))
    lines, errors, ok = build_edition(day, datetime.now())
    if debug:
        for key in sorted(errors):
            exc = errors[key]
            print(f"{key}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if ok == 0:
        print(f"shinbun: every section failed — is tor running? "
              f"(left {day}.txt alone)", file=sys.stderr)
        return 1
    out = DATA_DIR / f"{day}.txt"
    write_atomic(out, "\n".join(lines) + "\n")
    print(f"shinbun: archived {day} -> {out}")
    return 0


def mode_read_article(arg, use_pager, debug):
    """Pull, strip, archive, and page ONE NPR article. Accepts the bare
    code (nx-s1-…), a /path, or a full URL — only the tail is the code.
    Re-reads of an archived article are LOCAL."""
    code = arg.rstrip("/").split("/")[-1]
    if not code:
        print("shinbun: usage: shinbun --read <code> (the › codes in the "
              "edition's NPR section)", file=sys.stderr)
        return 1
    art = DATA_DIR / "articles" / f"{code}.txt"
    if art.exists() and art.stat().st_size > 0:
        emit(art.read_text().rstrip("\n"), use_pager)
        return 0
    try:
        doc = http_get(f"{NPR_BASE}/{code}")
        body = trim_article(render_html(doc))
    except Exception as exc:
        if debug:
            print(f"read: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"shinbun: fetch failed for {code} "
              f"({NPR_BASE}/{code}) — tor down?", file=sys.stderr)
        return 1
    if not any(l.strip() for l in body):
        # a failed/empty render dies loudly and leaves NO stamp-only file
        print(f"shinbun: empty render for {code} ({NPR_BASE}/{code})",
              file=sys.stderr)
        return 1
    # the pull stamp rides in the archived file itself, so offline re-reads
    # still show when and how it was pulled
    stamp = style(f"  {code} · pulled "
                  f"{datetime.now().strftime('%Y-%m-%d %H:%M')} "
                  f"via {via_word()}", "masthead")
    text = "\n".join([stamp, ""] + [clip(l, WIDTH) for l in body]) + "\n"
    write_atomic(art, text)
    print(f"shinbun: archived {code} -> {art}", file=sys.stderr)
    emit(text.rstrip("\n"), use_pager)
    return 0


# ----------------------------------------------------------------------- main


def build_parser():
    ap = argparse.ArgumentParser(
        prog="shinbun",
        description="the daily world-news edition, pulled over tor, "
                    "read offline")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--archive", nargs="?", const="", metavar="YYYY-MM-DD",
                       help="pull the edition (default date: today)")
    modes.add_argument("--read", metavar="CODE",
                       help="pull, archive, and page one NPR article")
    ap.add_argument("--strict", action="store_true",
                    help="tor unreachable = fail closed (default: warn and "
                         "fall back to clearnet)")
    ap.add_argument("--no-tor", action="store_true",
                    help="deliberate clearnet pull, no warning")
    ap.add_argument("--jitter", type=int, default=0, metavar="MINUTES",
                    help="sleep a random 0..MINUTES minutes before pulling "
                         "(--archive only; for cron scheduling)")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color (NO_COLOR is also honored)")
    ap.add_argument("--no-pager", action="store_true",
                    help="write straight to stdout instead of $PAGER")
    ap.add_argument("--debug", action="store_true",
                    help="report why sources failed, on stderr")
    return ap


def main(argv=None):
    global WIDTH, COLOR, STRICT, ROUTE
    ap = build_parser()
    args = ap.parse_args(argv)
    WIDTH = max(MIN_WIDTH, min(80, shutil.get_terminal_size().columns))
    STRICT = args.strict
    if args.no_tor:
        ROUTE = "clearnet"
    # The edition is a file read back later, so color follows the flags,
    # not whether this run happens to have a tty (a timer never does).
    COLOR = not args.no_color and "NO_COLOR" not in os.environ
    use_pager = sys.stdout.isatty() and not args.no_pager
    if args.jitter:
        if args.jitter < 0:
            ap.error("--jitter must be >= 0")
        if args.archive is None:
            ap.error("--jitter only makes sense with --archive")
    if args.archive is not None:
        day = args.archive or date.today().isoformat()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            ap.error(f"bad date: {day} (want YYYY-MM-DD)")
        return mode_archive(day, args.jitter, args.debug)
    if args.read is not None:
        return mode_read_article(args.read, use_pager, args.debug)
    return mode_read(use_pager)


if __name__ == "__main__":
    sys.exit(main())
