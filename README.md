```text
███████╗██╗  ██╗██╗███╗   ██╗██████╗ ██╗   ██╗███╗   ██╗
██╔════╝██║  ██║██║████╗  ██║██╔══██╗██║   ██║████╗  ██║
███████╗███████║██║██╔██╗ ██║██████╔╝██║   ██║██╔██╗ ██║
╚════██║██╔══██║██║██║╚██╗██║██╔══██╗██║   ██║██║╚██╗██║
███████║██║  ██║██║██║ ╚████║██████╔╝╚██████╔╝██║ ╚████║
╚══════╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚═════╝  ╚═════╝ ╚═╝  ╚═══╝
  新聞 · world news over tor · one file, stdlib only
```

![python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-none-44CC11)
![tor](https://img.shields.io/badge/tor-socks5h-7E4798)
![tests](https://img.shields.io/badge/tests-35%20offline-89E051)

**shinbun** (新聞) is Japanese for "newspaper". It pulls ONE dated 80-column
plaintext "daily edition" of world news over Tor each morning and reads it
back offline. No app state, no inbox, no unread counts: a jittered timer
pulls the edition, the read path is a local file plus `$PAGER` with zero
network traffic, and yesterday's file is the archive. Grep and your editor
work over the archive directory like any other plaintext.

It is a single Python file, standard library only: no dependencies, no API
keys, no config. The HTTP layer (a hand-rolled SOCKS5 client plus a minimal
HTTP/1.1 reader) and the HTML renderer are in the file, so the whole program
can be audited in a sitting.

---

## ⚡ 30-second demo

```bash
git clone https://github.com/v0idravl/shinbun.git
cd shinbun
python3 shinbun.py --archive && python3 shinbun.py
```

Example edition excerpt (`--no-color`):

```text
   THE DAILY EDITION — 2026-09-06 · pulled 05:41 via tor

 NPR ───────────────────────────────────────────────────────────────────────────
Stub & Headline
                                                                       › nx-s1-1

Second Headline
                                                                       › nx-s1-2

 WORLD ─────────────────────────────────────────────────────────────────────────
 1. World Stub One
                                                         https://aje.example/one
 2. World Stub Two
                                                         https://aje.example/two

 HACKER NEWS ───────────────────────────────────────────────────────────────────
 1. Stub HN One
                                                         https://example.com/one
 2. Stub HN Two & more
                                                         https://example.com/two

 CURRENT EVENTS ────────────────────────────────────────────────────────────────
Topics in the news

 ── September 2, 2026 (Wednesday) ──

- A thing happened in a place with some details attached.

- Another thing happened elsewhere entirely.
```

In a terminal the masthead is bold blue, section rules cyan, and the day
separators yellow — named 16-color ANSI only, so the edition tracks your
terminal palette. Bodies stay plain text so grep over the archive is clean.

---

## ⌨️ Usage

```bash
shinbun                        # page the freshest LOCAL edition — zero network
shinbun --archive              # pull today's edition
shinbun --archive 2026-09-01   # pull (or repull) a specific date
shinbun --read nx-s1-12345     # pull, archive, and page one NPR article
shinbun --strict               # tor unreachable = fail closed
shinbun --no-tor               # deliberate clearnet pull, no warning
shinbun --jitter 90            # sleep a random 0–90 min before pulling (cron)
shinbun --no-color             # plain text (NO_COLOR is also honored)
shinbun --no-pager             # straight to stdout, for tmux and scripts
shinbun --debug                # explain failed sources on stderr
```

The read path pages today's file if it exists, else the newest edition on
disk; an empty archive directory is a loud hint to run `--archive`. `--read`
accepts the bare code, a path, or a full URL; re-reads of an already
archived article are local. Editions land in
`~/.local/share/shinbun/YYYY-MM-DD.txt` (override with `SHINBUN_DATA_DIR`),
articles in `articles/<code>.txt` with a pull stamp as their first line.
Writes are atomic (temp file plus rename), and an all-sources-dead pull
never clobbers a good edition.

In a terminal the edition goes through `$PAGER` (default `less`), which gets
`-RFX` so color survives and quitting leaves the page in your scrollback; a
missing pager falls back to plain stdout. Width follows the terminal from 80
columns down to 48; anything still too long is clipped rather than wrapped.

---

## 📰 Sections

| Section | Source | Contents |
|---|---|---|
| NPR | text.npr.org index | headlines, each with its short code (`nx-s1-…`) right-locked to column 80 — the `--read` handle |
| WORLD | Al Jazeera RSS | top 15 numbered title/link pairs, URL right-locked in quiet blue under each, a blank line between items |
| HACKER NEWS | hnrss.org frontpage | top 20 numbered title/link pairs, same format |
| CURRENT EVENTS | Wikipedia Portal:Current events | the portal render trimmed to the news, each day's date heading wrapped in a yellow rule |

Each source fails soft independently: a dead source marks its section
`unavailable` and the rest of the edition still renders. Exit status is
nonzero only when every source fails (in which case no file is written).

---

## 📡 Data sources

All sources are keyless, and only HTML/RSS text is ever fetched.

| Source | Provides |
|---|---|
| [NPR text-only](https://text.npr.org/) | headline index + full articles for `--read` |
| [hnrss.org](https://hnrss.org/) | Hacker News front page as RSS |
| [Al Jazeera RSS](https://www.aljazeera.com/xml/rss/all.xml) | world headlines |
| [Wikipedia Portal:Current events](https://en.wikipedia.org/wiki/Portal:Current_events) | the daily current-events portal, via `action=render` |

---

## 🧅 Tor model

- Every fetch rides the local tor daemon's SOCKS port (`127.0.0.1:9050`,
  override with `SHINBUN_SOCKS`) with **socks5h semantics**: the CONNECT
  request carries the domain name (ATYP=3) and the *exit node* resolves DNS,
  so a lookup never lands in the local resolver's log.
- The User-Agent is Tor Browser's blend-in value (a stock Firefox ESR
  string); a custom UA would fingerprint every pull. No cookies, no JS.
- Known limit: the TLS ClientHello is Python's `ssl` fingerprint, not
  Firefox's — a CDN can tell "script, not browser" (JA3/JA4). That identifies
  the tool class, never you: no IP, no account, no cookies, no day-to-day
  state. Hiding it would require a browser's TLS stack, which defeats the
  one-file design.
- **Traffic budget: exactly 4 GETs per edition pull** (one per source), text
  only — subresources and images are never fetched. The pulls are shaped
  against correlation: fetch order is shuffled per run (a `secrets`-based
  Fisher-Yates) and between fetches the pull sleeps a random 5–120 s, so the
  day's requests are neither a fixed burst nor a fixed sequence.
- If the SOCKS port does not answer, the default is **fail-soft with a
  warning**: a loud stderr message and a `via CLEARNET (tor unreachable)`
  line under the masthead, then the run continues direct. `--strict` fails
  closed instead; `--no-tor` is deliberate clearnet and stays silent.

---

## 🕰 Scheduling

The shipped example systemd **user** units pull the edition each morning:

```bash
mkdir -p ~/.config/systemd/user ~/.local/bin
cp shinbun.py ~/.local/bin/shinbun && chmod +x ~/.local/bin/shinbun
cp systemd/shinbun-archive.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shinbun-archive.timer
```

The timer is `OnCalendar=*-*-* 05:30:00` with `RandomizedDelaySec=6h` and
`Persistent=true`. The long jitter is deliberate: a fleet of news readers
all fetching at 05:30:00 sharp is a herd, and a fixed daily fetch time is a
fingerprint — the pull lands somewhere in the morning instead, and a missed
run (laptop asleep) catches up on next boot. The service runs
`--archive --strict`: an unattended pull should fail closed rather than
silently leak a clearnet fetch, and a failure just retries tomorrow.

Without systemd, cron plus `--jitter` does the same job:

```cron
30 5 * * *  shinbun --archive --strict --jitter 360
```

---

## 🗃 Browsing the archive

```bash
ls ~/.local/share/shinbun/                  # one YYYY-MM-DD.txt per day
less ~/.local/share/shinbun/2026-09-06.txt  # read a past edition
grep -ri keyword ~/.local/share/shinbun/    # search everything, articles too
```

---

## 🛠 Requirements

Python 3.8+. Nothing else: stdlib only, no third-party packages. A running
tor daemon is recommended; without it shinbun warns and pulls over clearnet
unless you pass `--strict`.

---

## 🧪 Tests

```bash
python3 -m unittest test_shinbun -v
```

35 tests, all offline. Fetchers are exercised against canned fixtures
through an injectable getter, so the suite never touches the network; the
one exception is loopback sockets in the SOCKS5 wire-format test, which runs
a fake SOCKS server on 127.0.0.1 and asserts the CONNECT request carries
ATYP=3 and the ASCII domain name.
