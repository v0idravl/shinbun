# AGENTS.md

Guidance for AI coding agents working in this repository. Assumes no prior
knowledge of the project.

## Project overview

**shinbun** (新聞, "newspaper") is a single-file, stdlib-only Python terminal
program that pulls ONE dated 80-column plaintext "daily edition" of world
news over Tor each morning and reads it back offline. Sections: NPR
headlines (text.npr.org), WORLD (Al Jazeera RSS), DEUTSCHE WELLE (DW world
RSS), UN NEWS (UN News RSS), HACKER NEWS (hnrss.org), and CURRENT EVENTS
(Wikipedia Portal:Current events). The read path is a
local file plus `$PAGER` with zero network traffic; yesterday's file is the
archive.

- **Requirements:** Python 3.8+. No third-party packages, no API keys, no
  configuration files. A local tor daemon is recommended; without it the
  default is warn-and-fall-back to clearnet (`--strict` fails closed,
  `--no-tor` is deliberate clearnet).
- **Entry point:** `shinbun.py` (the whole application is this one file).
  Modes: no args (page the freshest local edition), `--archive [DATE]`
  (pull), `--read <code>` (pull + archive + page one NPR article).
  Flags: `--strict`, `--no-tor`, `--jitter MINUTES`, `--no-color`,
  `--no-pager`, `--debug`.
- **Repository contents:**
  - `shinbun.py` — the entire application
  - `test_shinbun.py` — the full test suite (46 tests, offline)
  - `README.md` — user-facing docs; keep it in sync with behavior changes
  - `systemd/` — example USER units (`shinbun-archive.service` + `.timer`)
  - `LICENSE`, `.gitignore`
- There is **no build system** (no pyproject.toml, setup.py, Makefile, or CI
  config). The file is run in place or copied to `~/.local/bin/shinbun`.

## Architecture

`shinbun.py` is organized top-to-bottom in banner-comment sections:

1. **Header docstring** — usage, the why, the tor model, and the traffic
   budget (exactly 6 GETs per edition pull, one per source, text only —
   subresources and images are never fetched). Keep it in sync.
2. **Constants** — the blend-in `UA` (stock Firefox ESR string; a custom UA
   would fingerprint every pull), source URLs, `SOCKS_HOST`/`SOCKS_PORT`
   (`SHINBUN_SOCKS`, default 127.0.0.1:9050), `DATA_DIR`
   (`SHINBUN_DATA_DIR`, default `~/.local/share/shinbun/`), `WIDTH` (80 cap,
   48 floor; `main()` narrows it to the terminal), and the mutable run state
   (`COLOR`, `STRICT`, `ROUTE`, `FELL_BACK`) that `main()` sets and tests
   mutate. Also `sleep()`, the one sleep seam: tests patch it and
   `SHINBUN_NO_JITTER=1` zeros it.
3. **ANSI** — the `ANSI` palette (named 16-color bold only, so editions
   track terminal palettes), `style()`/`visible_len()`/`clip()`
   (ANSI-aware truncation; `build_edition()` clips every line as the width
   backstop EXCEPT bare right-locked URL lines via `clip_line()` — a clipped
   link can be neither opened nor grepped), `rule()` (cyan ` LABEL ───` section rules to WIDTH), and
   `right_lock()` (pads so a string ENDS at column WIDTH — by character
   count, so the multibyte `›` lands correctly).
4. **HTTP over tor** — `http_get(url)` is THE one choke point for every
   fetched byte: a hand-rolled SOCKS5 client (greeting, then CONNECT with
   ATYP=3 — the domain name goes to the exit node, which resolves DNS;
   nothing ever lands in the local resolver's log), an
   `ssl.create_default_context()` wrap, a minimal HTTP/1.1 request
   (gzip content-encoding decoded — some CDNs force it regardless of
   `Accept-Encoding` — `Connection: close`), a chunked-transfer decoder, ≤5 redirects, UTF-8
   with replacement. `_open_connection()` makes the tor-vs-clearnet
   decision ONCE per run: if the SOCKS port itself does not answer, the run
   warns loudly on stderr, sets `FELL_BACK` (the masthead gets a
   `via CLEARNET (tor unreachable)` line), and continues direct — unless
   `STRICT`, in which case it exits. A failed SOCKS *handshake* (tor up,
   exit can't reach the host) is a per-host failure, not a fallback
   trigger. `FetchError` is the fail-soft exception.
5. **HTML → text** — `TextRenderer` (an `html.parser.HTMLParser`, a poor
   man's `w3m -dump`): block-level line breaks, `- ` bullets with hanging
   indent, script/style skipped, ASCII whitespace collapsed (NBSP
   deliberately preserved — the portal's date headings use it), blocks
   wrapped to WIDTH, `convert_charrefs=True`. `NprIndexParser` extracts the
   `<a class="topic-title">` rows (attribute-order agnostic).
   `parse_rss()` is ElementTree item → title/link, shared by HN, Al
   Jazeera, DW, and UN News.
6. **Portal trims** — `trim_portal()` (start at "Topics in the news", stop
   at "More <Month> events..."/"Ongoing events"; each trim fail-soft),
   `strip_furniture()` (edit/history/watch stubs, "Nominate an article";
   both `•` and `-` bullet flavors), `day_separators()` (NBSP-normalized
   date headings wrapped in yellow rules — the portal spans today AND
   tomorrow), `trim_article()` (NPR's "Text-Only Version" header line and
   everything from a bare "Topics" line down).
7. **Sources** — one `src_*` function per section, each returning a list of
   lines and raising `FetchError` on an empty/unparseable pull so a reshaped
   upstream page fails soft instead of writing an empty section. The
   `SOURCES` table is the registry: adding a source is a row here plus a
   function.
8. **Edition** — `build_edition()` fetches the sources with a
   secrets-random FETCH_GAP_MIN..MAX (5–120 s) sleep between them, in a
   per-run secrets-shuffled fetch order (the day's six GETs are neither a
   fixed burst nor a fixed sequence; RENDER order stays fixed), composes
   the masthead AFTER the fetches so it tells the
   truth about the route taken, and returns `(lines, errors, ok_count)`.
   `collapse_blanks()` keeps furniture-stripping from leaving holes.
   `write_atomic()` is temp-file + `os.replace`.
9. **Pager** — `pager_command()`/`emit()`: `$PAGER` (default `less`) gets
   `-RFX` and a `LESSUTFCHARDEF` declaring the Nerd Font Private Use Area
   printable (the masthead glyph lives there); a missing pager falls back
   to stdout. The pager is the ONLY child process the program ever spawns.
10. **Modes / main** — `mode_read()` (today's file else newest on disk,
    empty dir = loud hint; zero network), `mode_archive()` (jitter sleep
    first, anti-clobber: all sources dead = no write + nonzero exit),
    `mode_read_article()` (bare code / path / URL; archived re-reads are
    local; a failed or empty fetch dies loudly and leaves no stamp-only
    file), argparse in `build_parser()`/`main()`.

Runtime behavior worth knowing:

- The edition is a file read back later, so COLOR follows the flags
  (`--no-color`/`NO_COLOR`, honored by presence), NOT whether the run has a
  tty — a timer never does.
- Exit status is nonzero only when every source fails (archive), on a
  failed/empty article fetch (read), or on usage errors.
- `--jitter` sleeps `secrets.randbelow(MINUTES*60 + 1)` seconds BEFORE
  pulling and is rejected without `--archive` (it is the cron substitute
  for systemd's RandomizedDelaySec).

## Build and test commands

```bash
python3 shinbun.py --archive && python3 shinbun.py   # pull, then read
python3 -m unittest test_shinbun -v                  # full suite (offline)
```

There is nothing to install and nothing to build. There is no linter or
formatter configured in the repo; match the existing style by eye.

## Testing instructions

- The suite is plain `unittest`, runs fully offline, and currently has 46
  tests. Run it before and after any change. A test that touches the real
  network is a regression: every source function takes an injectable
  `_get(url)` getter, and `main()`-level tests patch `shinbun.http_get`.
  The ONE exception is loopback: the SOCKS5 wire-format test runs a fake
  SOCKS server on 127.0.0.1 and asserts the CONNECT request carries ATYP=3
  and the ASCII domain name (remote DNS). Preserve both seams.
- Tests redirect `shinbun.DATA_DIR` into a `TemporaryDirectory` and reset
  the module state (`WIDTH`, `COLOR`, `STRICT`, `ROUTE`, `FELL_BACK`) in
  `setUp`/`tearDown`; `shinbun.sleep` is patched. Follow that pattern.
- `setUpModule`-level env: `COLUMNS=80` (stable width assertions) and
  `SHINBUN_NO_JITTER=1`.
- Width assertions use `visible_len()` (strips ANSI); new output must
  respect `WIDTH` — `build_edition()` clips as the backstop.

## Code style guidelines

- Standard library only, Python 3.8 compatible: no match statements, no
  `X | Y` type hints, no `str.removeprefix`-era APIs. No subprocess for
  fetching or rendering (no curl, no w3m) — the pager is the only child.
- Style is idiomatic, compact PEP 8: 4-space indent, ~80-column lines,
  double-quoted strings, no type annotations. Comments explain WHY
  (rationale, failure modes), not what — banner-comment sections, a short
  paragraph above a constant block or a docstring only where behavior is
  non-obvious.
- Sources normalize to line lists; renderers are pure where possible. Keep
  the fetch/parse/render split.
- All display output goes through `style()` so `--no-color` and `NO_COLOR`
  keep working; measure width with `visible_len()`.
- Unknown upstream shapes degrade fail-soft (a marker gone = that trim
  passes through; an empty parse raises `FetchError` so the section marks
  itself unavailable rather than writing nothing).

## Security considerations

- The tor boundary is the point of the program: socks5h (remote DNS) on
  every fetch, the blend-in UA, no cookies, a 6-GETs-per-day traffic
  budget. Do not add a clearnet fast path, subresource fetching, or a
  custom User-Agent.
- The default tor-down behavior is fail-SOFT-WITH-WARNING by design (a
  morning paper that never prints is its own failure); `--strict` is the
  fail-closed mode and is what the shipped systemd unit uses. Keep both.
- The TLS context is `ssl.create_default_context()` with
  `server_hostname` set — do not weaken verification.
- No secrets, tokens, or credentials exist anywhere in this project;
  keep it that way.
