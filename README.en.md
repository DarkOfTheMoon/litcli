# litcli — search, cite and fetch scholarly literature

[简体中文](README.md) · [English](README.en.md)

> Gitee ([`simult/litcli`](https://gitee.com/simult/litcli)) is the release home; this GitHub repo is a
> mirror that runs CI, so [downloads live on Gitee](https://gitee.com/simult/litcli/releases) (each release
> ships a wheel, an sdist and `SHA256SUMS`). Full detail is in the [Chinese README](README.md) and
> [AGENTS.md](AGENTS.md).

One record model across many sources: your local Zotero library, arXiv, Crossref, OpenAlex, the Chinese
CNKI index, Douban books, and open-access corpora (DOAJ, Europe PMC, DOAB, Zenodo). Built for you and
your agent: search, read full text, export CSL JSON that feeds `pandoc --citeproc`, write back to your
own library.

- **No Better BibTeX needed** — `export --format csljson` produces CSL JSON directly.
- **No zotero.org account** — writes go through Zotero 10's local API; one runtime authorization.
- **Works without Zotero** — the public backends alone can search, fetch and export.
- **No captcha bypassing** — proof-of-work walls are solved locally with `hashlib`, human sliders are
  handed back to you, and results that are public copies without an open licence are labelled as such.

## Quick start (no Zotero required)

```bash
python3 -m venv .venv && . .venv/bin/activate                    # or use an environment you have
pip install .                                                    # Debian/Ubuntu PEP 668: use ./install.sh
litcli search "graph neural network" --limit 5            # every source by default; column 1 is `source/key`
litcli data get 10.1038/s41598-024-80509-9 --backend crossref --format json
litcli export 10.1038/s41598-024-80509-9 --backend crossref --format csljson
litcli fetch 10.1038/s41598-024-80509-9            # saved to the current directory; stdout is the path
```

`search` covers every source by default; `--backend` narrows it. `--format json` prints one record per line and an
empty result prints `[]`. The table marks what kind of copy a row is right after the key — `[oa]` openly
licensed, `[entitled]` a subscription copy through your institution, `[public]` a public copy without an
open licence, `[local]` your own library; JSON has the same in `access`.
When `[public]` rows appear, one stderr line says so: those are copies from environments such as Sci-Hub / Z-Library,
**not an open-access licence**, and checking the licence before reuse is on you rather than on the tag.
`fetch` writes into the current directory (`--out DIR` overrides), prints only the path
on stdout and its reasons on stderr; exit codes `0` file saved / `1` nothing found / `2` error.

## Install

**Preferred: the binary installer — no Python, no source checkout.** Releases ship an
installer with a `SHA256SUMS` next to it.

```bash
# Linux: self-extracting, installs under ~/.local/opt/litcli-<version>, links ~/.local/bin/litcli
sh litcli-<version>-linux-x86_64.run        # --with-browser also downloads Chromium for CNKI/Douban
sh litcli-<version>-linux-x86_64.run --uninstall
```

On Windows run `litcli-<version>-windows-x86_64-setup.exe` (per-user install under
`%LOCALAPPDATA%\Programs\litcli`, no admin); the Chromium download is an optional wizard checkbox.

The installer holds frozen bytecode and shared objects — **no `.py` sources** — and needs no Python
on the target machine. Chromium is not bundled (~150 MB per engine); install it when using CNKI or
Douban, from the wizard or later:

```bash
litcli backends install-browser
litcli backends install-browser --check   # self-test: launch it headless and report the version
```

Browsers live in Playwright's own cache (`~/.cache/ms-playwright` /
`%LOCALAPPDATA%\ms-playwright`), survive upgrades and are kept on uninstall.

**From source** (to change code or run the tests):

```bash
pip install .                        # into the current environment (venv, Python 3.11+)
./install.sh                         # Debian/Ubuntu PEP 668: --user snapshot into ~/.local
export PATH="$HOME/.local/bin:$PATH"  # if ~/.local/bin is not on PATH yet (persist it in your shell rc)
litcli backends                      # self-test: configured backends and their capabilities
```

`install.sh` copies the source into `~/.local` and decouples production from the checkout; rerun it to
upgrade. Development uses `./bin/litcli` in the repo. PDF text extraction needs `pdftotext`
(poppler-utils) and degrades to empty text without it. CNKI and Douban additionally need
`./install.sh --with-cnki` / `--with-douban` (Playwright).

**Windows, from source**: `install.sh` and the completion scripts are POSIX-only; use
`py -m venv .venv`, `.\\.venv\\Scripts\\Activate.ps1` and `pip install .`, or `bin\\litcli.cmd`
inside the checkout. In-repo development on Linux uses `./bin/litcli`. PDF previews need
`pdftotext.exe` from poppler on `PATH`; completion covers bash/zsh/fish only.

Config lands per platform (`%APPDATA%\litcli\config.toml` on Windows, `~/.config/litcli/`
elsewhere). Upgrading means rerunning `./install.sh` (it overwrites the snapshot); uninstall with
`pip uninstall --break-system-packages litcli` (PEP 668 machines; not needed in a venv) and remove
`~/.config/litcli` and `~/.local/share/litcli`.

Everything stays local: citations go into your own Zotero library, config into
`~/.config/litcli/config.toml`, and the only outbound traffic is to the upstream APIs. `install.sh`
also distributes `skills/litcli/SKILL.md` to any detected agent (Claude Code / Codex / Cursor /
Gemini) and keeps a neutral copy under `~/.local/share/litcli/skill/`. `--no-skills` skips them,
`--skills-only` installs nothing but skills, and `--skills claude,cursor` targets named agents.

## Commands

`--backend` narrows to one source (unset means every configured source plus the keyless OA corpora); a key may
also carry its source prefix (`openalex/W123`, `zotero/AB12`), which routes by prefix with `--backend` still
winning. `--all` and `--backend` are mutually exclusive. Aggregation de-duplicates (same work across sources
becomes one row, DOI/ISBN first) and tags `extra["sources"]`. The first column of a result row is `source/key`;
both forms are accepted. Exit codes: `0` success (including no results), `2` error or unsupported backend,
`1` for `fetch` meaning "no fulltext".

Search fans out: **a single failing source is silent by default** — `--verbose` (or `LITCLI_VERBOSE=1`) names
which source failed and why. When every source failed it says so even without the flag and exits `2`, since that
is not "nothing found". Commands whose result you need (`fetch`, `data`, `export`, `refs`, `citing`, `related`,
`backends`) always speak up on failure. With the local Zotero app closed, reads and writes fail with a connection
error immediately (no timeout wait); `backends status` probes the port first and marks `[unreachable]`, instead of
letting you answer a y/N prompt first.

Metadata reads are cached for a day (`~/.cache/litcli/cache`; successful GETs only, no files, no writes),
so repeating a search does not spend the site's budget again — use `--no-cache` for fresh results. Sources are
queried a few at a time (up to four) under a 45-second overall limit, so a stuck source is given up on instead of
holding the command. `search`, `fetch`, `export`, `refs`, `citing`, `related` are read-only;
`data` writes.

The table adds a short abstract snippet under each title (160 chars), because a title alone cannot be judged.
Results are ordered by relevance (a title hit outweighs an abstract hit, covering every token adds more, ties go
to citations then year). Spellings of one thing count as the same word: plurals, a part of a compound
(`viscoelastic` carries `elastic`), and same-topic aliases of a method or setting (reduced-order / low-order / ROM /
PCA / LES, combustor / combustion, microchannel / microfluidic), with a spelled-out method name
(`principal component analysis`) compressed to its acronym before comparing, so `--limit` keeps the rows that answer the query rather than whichever source replied
first; `--sort cited` switches to a pure citation ranking. Field filters are sent to the backends and re-checked
locally, so a source that ignores one cannot leak records. A citation badge reads `[cited N via source]` and says
`(sources disagree)` when sources report different counts.

### How each backend maps a filter

A backend without the field folds it into free text, or ignores it:

- **crossref** → `query.title` / `query.author` / `query.container-title` / `filter=from-pub-date,until-pub-date`
- **openalex** → `filter=title.search:` / `raw_author_name.search:` / `publication_year:`; `container` folds into free text
- **arxiv** → `ti:` / `au:` / `submittedDate:[…]`, AND-joined into `search_query`
- **zotero** → everything folds into a single `q` (`qmode=everything`, all fields)

**Commands** (read-only unless marked as a write)

| Command | What it does |
|---|---|
| `litcli search "X" [--all] [--backend B] [--type T] [--limit N] [--no-cite] [--format table\|json\|csl]` | candidate search; field filters and sorting above; `--no-cite` omits the in-table citation |
| `litcli refs <key>` / `citing <key>` / `related <key>` (all take `--limit N`, `--sort cited`, `--format table\|json\|csl`) | cited works / citing works / semantic neighbours — the sources are asked in preference order (openalex → semanticscholar → opencitations → crossref → europepmc); `refs` and `related` keep asking the rest and merge them in by identity up to `--limit` (the citation indexes overlap too much for that to pay off on `citing`), a throttled one says `[skipped]` and the next is asked, and `--backend` or a `source/` prefix pins one source with no merging; with no neighbour list `related` falls back to a title-term search and says so |
| `litcli export <key>... [--format csljson\|bibtex\|ris\|text] [--no-cite]` | citations; `csljson` feeds `pandoc --citeproc`. arXiv keys normalise to a DOI only with `--backend openalex\|crossref` |
| `litcli fetch <DOI or publisher article URL> [--out DIR] [--no-download] [--lines N] [--email ADDR] [--scihub]` (the old `fetch fulltext <doi>` and the `--download` flag are still accepted) | save the fulltext: local store → Unpaywall → OpenAlex → Europe PMC → Zenodo → each publisher's subscription copy (tried automatically for its DOI prefixes, see the table) → landing page; `--lines` also previews the first N text lines, `--email` is the Unpaywall/OpenAlex polite-pool address |
| `litcli fetch --book "title\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--limit N] [--pick N]` | book candidates → pick → full metadata → save the file (an ISBN takes this path automatically); `--limit` candidates, `--pick` chooses one |
| `litcli data get <key> [--format table\|json\|csl]` · `data tree` | read one record; browse the collection tree |
| `litcli backends list` · `backends status` | diagnostics: configured backends and capabilities / authorization and reachability |
| `litcli completions <bash\|zsh\|fish>` | print a self-contained completion script; `eval "$(...)"` then TAB |
| `litcli data members <coll> [--no-recursive] [--limit N] [--format table\|json]` | list a collection's records (recurse by default, `--no-recursive` lists one level) |
| `litcli backends install-browser [--check] [--dry-run] [--with-deps]` | download the browser; `--check` launches it headless, `--dry-run` only shows the source, `--with-deps` apt-installs OS libraries |
| `litcli --version` | print the version and exit |
| `litcli --no-cache <command>` | bypass the response cache (`LITCLI_NO_CACHE=1` does the same); entries live 24 h and only successful metadata reads are kept |
| `litcli --verbose <command>` | per-source failure lines for a search (silent by default; `LITCLI_VERBOSE=1` does the same), stderr only |
| `litcli --debug <command>` | one line per request, status, duration, pacing wait and retry on stderr (`LITCLI_DEBUG=1` does the same); unexpected exceptions also print the stack. stdout is untouched |

**Writes** (Zotero only; read-only backends answer `[skipped]` and exit 2)

| Command | What it does |
|---|---|
| `litcli backends authorize zotero` | one-off write authorization (local dialog) |
| `litcli data add --title "..." --creator "Family, Given" [--year ..] [--doi ..] [--type ..] [--url ..] [--abstract ..] [--force]` / `data add --pdf\|--csl <file> [--yes]` | ingest metadata (dedupe gate, `--force` skips), heuristics from page 1 (y/N unless `--yes`), or CSL JSON |
| `litcli data set <key> --title ..` (also `--abstract/--doi/--url/--year`) · `data del <key>` · `data attach <key> <path\|URL>` | edit, delete, attach a file |

### Surveying a subject

A survey turns "collect the literature around a subject" into a long-running project: `survey new`
writes the project (seeds, year window, journal constraint, evidence floor, effort level) to
`project.toml` (an opening search where every hit is off the subject is retried ten rows per source deep
and, if still nothing clears the floor, refused with the candidates listed), `survey run` casts the citation graph one hop at a time, lands every admitted
fulltext and its metadata in `corpus/`, and records every step in `trace.jsonl` so a walk can be
resumed and audited.

| Command | What it does |
|---|---|
| `litcli survey new "<subject>" [--seed DOI|TITLE]... [--year A-B] [--type T] [--container JOURNAL] [--floor 0..1] [--effort LEVEL]` | create a project; prints only its name; without `--seed` it first searches and takes the first 3 hits as seeds — all off the subject, it reads ten rows per source, and with nothing above the floor it exits 2 and lists the candidates with their DOIs for `--seed` |
| `litcli survey run <project> [--effort LEVEL] [--format jsonl\|md]` | resume from the last checkpoint; stdout is JSONL events, `--format md` prints one table per hop instead |
| `litcli survey status <project>` | one JSON line: question, status, floor (`floor_legacy` names a migration from `threshold`), seeds (with their question coverage — `?` when the title is unknown — and `weak`), hops, admitted, last stop (with what it left unexpanded and the best evidence), remaining quota; `--why` switches to a text audit view |
| `litcli survey stop <project>` | write `$.stop`; the walk ends at its next hop boundary |

The level decides how wide to walk, not how much to collect — a hot subject keeps turning up
above-floor works and runs to its quota or saturation, a cold one stops itself after a few barren
hops (a row the admit cap demoted but the floor let through stays on the frontier as an expansion
point — not collected, not downloaded):

| Level | frontier/hop | admit/hop | edges/work | barren hops to stop | openalex cap | arxiv cap | per publisher | max hops |
|---|---|---|---|---|---|---|---|---|
| low | 4 | 2 | refs 10 / citing 10 / related 5 | 2 | 40 | 10 | 2 | 6 |
| medium | 8 | 5 | 20 / 20 / 10 | 3 | 200 | 40 | 6 | 20 |
| high | 16 | 8 | 30 / 30 / 15 | 4 | 600 | 100 | 15 | 50 |
| max | 24 | 12 | 50 / 50 / 25 | 6 | 1500 | 200 | 30 | 120 |

The quota counts requests: a cache hit costs nothing, a retry still costs. One `run` per project
at a time (an existing `$.lock` exits 2); resume with the same command — expanded works are never
expanded twice. Every `seen.jsonl` row carries its reason (`score 3.21 >= 2.50`, `seen`,
`year … outside …`), and a failed download is marked `unavailable` without dropping the
candidate. `run` exits 1 when not one copy landed in `corpus/` — metadata sidecars do not count — and 130/143 when a second Ctrl-C/SIGTERM aborts it, with the status settled to `stopped` and the same command resuming.

## Backends and what they may fetch

Zotero, arXiv, Crossref and OpenAlex are enabled by default. Every other backend works with a one-off `--backend`
without any config. The keyless OA corpora (`doaj`/`europepmc`/`doab`/`zenodo`) need no config to take part in
`--all` and the default `fetch` chain; `config.toml` is for tuning them or opting other backends (`uspto`,
`cnki`, `douban`) into the aggregation.

| Backend | Access | What is fetched |
|---|---|---|
| `zotero` | read / write / attachment — your local library | local API, one write authorization |
| `arxiv` · `crossref` · `openalex` | free, no key | metadata (arxiv also PDF links); rate-limited and backed off |
| `doaj` · `europepmc` · `doab` · `zenodo` | open access, no key | public OA records (JATS XML / open files); europepmc also answers both graph directions |
| `semanticscholar` · `opencitations` | free, no key | citation graph: S2 answers all three axes (recommendations are the neighbour axis; an `api_key` raises the rate), COCI answers citing/cited DOI pairs only (CC0); the graph commands fall back to them without any config, while the `search` fan-out only carries them when their section exists in `config.toml` (the built-in keyless corpora are doaj/europepmc/doab/zenodo only) |
| `openlibrary` · `uspto` | free (uspto needs a free key) | book records / patents |
| `cnki` | CNKI web index | public metadata only, never paywalled full text; visible browser + manual slider |
| `douban` | Douban web | public bibliographic data; headless |
| `sciencedirect` | subscription content via your institution's entitlement (not OA) | fulltext PDF for Elsevier DOIs; tried automatically for the `10.1016`-style prefixes |
| `wiley` | subscription content via your institution's entitlement (not OA) | fulltext PDF for Wiley DOIs; tried automatically for the `10.1002`-style prefixes |
| `springer` | subscription content via your institution's entitlement (not OA); open-access articles need no config | fulltext PDF for Springer Nature DOIs; tried automatically for the `10.1007`-style prefixes |
| `cambridge` | subscription content via your institution's entitlement (not OA); CC-licensed open-access articles need no config | fulltext PDF for Cambridge DOIs; tried automatically for the `10.1017` prefix |
| `aip` | subscription content via your institution's entitlement (not OA); the site sits behind a Cloudflare challenge | fulltext PDF for AIP DOIs; tried automatically for the `10.1063` / `10.1121` prefixes |
| `acm` | subscription content via your institution's entitlement (not OA); Cloudflare challenge | fulltext PDF for ACM DOIs; tried automatically for the `10.1145` prefix |
| `aps` | subscription content via your institution's entitlement (not OA); Cloudflare challenge | fulltext PDF for APS DOIs; tried automatically for the `10.1103` prefix |
| `iop` | subscription content via your institution's entitlement (not OA); Cloudflare challenge | fulltext PDF for IOPscience DOIs; tried automatically for the `10.1088` prefix |
| `tandf` | subscription content via your institution's entitlement (not OA); Cloudflare challenge | fulltext PDF for Taylor & Francis DOIs; tried automatically for the `10.1080` prefix |
| `aiaa` | subscription content via your institution's entitlement (not OA); Cloudflare challenge | fulltext PDF for AIAA DOIs; tried automatically for the `10.2514` prefix |
| `ieee` | subscription content via your institution's entitlement (not OA); WAF challenge | fulltext PDF for IEEE Xplore items; tried automatically for the `10.1109` prefix |
| `scihub` | public copy (non-OA), DOI only | PDF, opt-in via `--scihub` (copying `config.example.toml` also enables it — the example keeps the `[backends.scihub]` section) |
| `zlibrary` | public copy (non-OA) | records + PDF; anonymous daily quota |

**China-specific sources.** CNKI drives a visible browser with your own logged-in profile and needs one
manual slider; Douban runs headless by default (its search page ships the records server-side) and only
drops to a visible browser if it flags your traffic. Neither is needed for the international sources.
A 403 from `europepmc.org` or DOAB means "no JS client", not "your IP is banned" — the CLI routes those
two around the wall; DOAJ has no wall at all. `www.sciencedirect.com` and
`onlinelibrary.wiley.com` (`[backends.wiley]`) are different: a Cloudflare check
plus a per-session entitlement decision (`CPE00001`), so it is not plug-and-play. Open the article once in
an **authenticated** browser tab (institutional login / campus network), then put that session's cookies and
the browser's own User-Agent into `[backends.sciencedirect]` (`cookie` / `cookie_file` + `user_agent` — both
are required: the clearance cookie is bound to the UA *and the egress IP*, and the server may drop it at any
time, so replaying it from another machine fails). The zone does not even accept a headless browser: the same
cookies and the same UA in headless Chromium still get `CPE00001` or a challenge page (measured 2026-11-24), so
only the session you minted in a *visible* browser counts. A tab that was not authenticated only offers "Purchase PDF",
and `/pii/` is silently rewritten to `/abs/`. Wiley is the same mechanism and the same setup
(`[backends.wiley]`; its clearance cookie may live a year yet is revoked with the session, and an unentitled
PDF URL is silently redirected to the abstract page). Wiley also offers a free **TDM token**
(`[backends.wiley].tdm_token`): that route uses `api.wiley.com`, bypasses Cloudflare entirely and only serves
what your institution subscribes to.
`link.springer.com` (`[backends.springer]`) has **no Cloudflare** and a different shape: the article page reads
anonymously, the metadata and the PDF link are always there, and open-access PDFs download with no config at
all. Only a paywalled PDF needs a session — its endpoint first 303s to `idp.springer.com` for an identity
cookie, and a session that is not entitled simply lands back on the article page (a silent downgrade, not a
403). That session is again a cookie plus its `user_agent` from an **authenticated** tab (bound to the UA;
refresh it when it expires).
`cambridge.org` (`[backends.cambridge]`) likewise has no Cloudflare: item pages read anonymously, the metadata
is always there and CC-licensed open-access PDFs download with no config. A paywalled item only reaches its
`/abs/` short page through `doi.org`, and its PDF endpoint 302s an anonymous client back to that page (a silent
downgrade); `fetch` then reports it and gives the link.

`pubs.aip.org` (`[backends.aip]`) is the opposite: it sits behind a Cloudflare *managed challenge*, so both the
page and the `article-pdf` endpoint answer a JS-less client with `403` and `cf-mitigated: challenge`. AIP is
therefore configured near-mandatorily with `[backends.aip]` `cookie` + `user_agent` (the `cf_clearance` cookie is
bound to both the UA and the egress IP; refresh it when it expires) — an unconfigured run reports that gate, not
a blocked IP.

ACM (`dl.acm.org`), APS (`link.aps.org`), IOPscience (`iopscience.iop.org`), Taylor & Francis
(`tandfonline.com`) and AIAA (`arc.aiaa.org`) share that Cloudflare shape, cookie plus `user_agent` included.
IEEE Xplore (`ieeexplore.ieee.org`, `[backends.ieee]`) sits behind a different gate — a WAF JS challenge
(an empty `202` body carrying `gokuProps`, cleared by `aws-waf-token`; since 2026-11 also an F5
`<APM_DO_NOT_TOUCH>` interstitial served with a `200` and the item title, released only by a `/TSPD/…` script and
cleared by `TS*` cookies) — and an unentitled session's PDF endpoint redirects to `/Xplore/login.jsp?…&authDecision=…`. All six take only institutionally subscribed or OA fulltext,
one DOI at a time, at low frequency.

**Rather than copying cookies, use the browser session**: with Chrome installed, run
`litcli browser login sciencedirect` (see `litcli browser status` for the site names), sign in
institutionally once in the window that opens, and leave the browser up — it outlives the
command, and closing that window ends the session (relaunching reuses the same profile).
`fetch` then pulls these publishers' fulltext through that live browser, taking both page and PDF in-page — the
credentials never leave the browser and renew themselves. `litcli browser status` reports each
site as `signed in` / `signed out` / `gated` / `no answer`. `gated` means the first paint was
the site's bot gate; a managed challenge swaps itself out, so it is waited out (and given one
more navigation) before the site is judged — a `gated` row is one the session really does not
get through. It is switched on by `[browser].enabled = true`
(off by default); while off the cookie path above runs unchanged, and a configured cookie keeps
working. The browser inherits `[network].proxy` — a clearance is bound to the egress IP, so both
must leave by the same one.
PDFs are judged by the page rather than by a clock: a page still showing the challenge is
waited out (up to five minutes), and the moment it is a document — an inline viewer, a page
redirected back to `/abs/`, a sign-in page — the wait stops and that page is the answer.
Only one browser per profile ever runs: a command attaches to the one already up (finding it
through `/proc` even when its discovery file was lost) and stops any duplicate on the profile,
and one it launched itself is stopped when the command ends — the one `login` left up is reused.
Back-to-back survey runs therefore do not pile up one browser each.
That session is also the fallback for **keyless open-access hosts**: some sites (MDPI, for one)
answer 403 to every client without JS site-wide, so a failed plain download is retried through
the browser. It is a fallback, not the main road — when it fails too, the plain failure is what
gets reported. Either way the command ends the same: every page is parked on `about:blank` and
the session is closed, so no dead unentitled tab (and no `--pdf-renderer`) is left behind; a
presigned asset address (`X-Amz-Expires`) also gets a note that it lapses within minutes.

## Configuration

`~/.config/litcli/config.toml` (`%APPDATA%\litcli\config.toml` on Windows; `$LITCLI_CONFIG`
overrides); with no file, a built-in default of Zotero + arXiv + Crossref + OpenAlex applies.
Every backend a file lists is enabled; to keep a section's fields but turn it off, write
`enabled = false` in it (this also covers the keyless OA corpora).
Every field is documented in `config.example.toml`.

```toml
[backends.zotero]
type = "zotero_local"
base = "http://localhost:23119/api"     # the write key is written by `litcli backends authorize zotero`

[network]
# proxy = "http://127.0.0.1:7890"       # campus / VPN; HTTP_PROXY / HTTPS_PROXY are read by default
```

## Known limits

- Unauthorized writes fail with `HTTP 401` (exit code 2) and never touch the library.
- Patent search needs a free USPTO key — patent records under keyless public sources are near zero.
- Europe PMC returns JATS **text**, not a typeset PDF; Unpaywall landing pages fall through to the end of the chain.
- arXiv may answer 406 in constrained sandboxes; `--all` skips failing backends and continues.
- Sci-Hub resolves DOIs only (no keyword search); Z-Library has an anonymous daily download quota, relaxed by
  setting `email`/`password`.
- ScienceDirect's PDF sits behind a **second acceptance gate** (`craft/challenge/pdf`): a page that opens does
  not guarantee the bytes. `fetch` downloads through the backend's session and, when gated, says so and prints
  the PDF link to open in your entitled browser. An open-archive (bronze) article carries no record-version
  `pdfft` anchor; the page offers an accepted-manuscript link under `/am/pii/…` instead. `fetch` takes it, and
  since that route redirects into the file itself, the bytes are read from where it landed (asking the route
  again only earns the site's bot check). Wiley's PDF endpoint answers an HTML abstract page (a silent
  redirect) instead of 403 for unentitled sessions, and `fetch` reports "not entitled / session expired" with
  the link. SpringerLink does the same: an unentitled session is sent back to the article page. A failed
  download on any of the three first finishes the chain's tail (legal landing page, `--scihub`).
- The binary installer ships no Chromium: CNKI/Douban need `litcli backends install-browser` first.
- `api_key`, Z-Library credentials and the publisher `cookie` / `cookie_file` values are stored in plain text;
  `backends authorize` chmods the config, hand-edited values and an exported cookies.txt are not covered — keep
  them out of public repositories. On Windows chmod only clears the read-only attribute — the `%APPDATA%`
  profile ACL is what isolates the file.

## Layout

```
litcli/          cli.py (entry: parse/dispatch/error mapping) + commands/ (one module per family) + backends/ (sources)
                 models.py the one normalized model · aggregate.py dedupe/merge · fulltext.py the chain · transport.py httpx primitives
tests/           unittest, fully offline (stub backends / MockTransport)
skills/litcli/   agent skill source; install.sh distributes it per target agent
```

Layers: domain (pure functions first) → transport → backends (read/write declared through `capabilities`) →
config → CLI. Layer duties and per-backend details are in [`AGENTS.md`](AGENTS.md), which is also the checklist
for a new backend.

## Development

```bash
python3 -m pip install -e '.[dev]'      # maintainer tools: build + a pinned ruff + pyinstaller (no runtime deps)
python3 -m unittest discover -s tests   # the whole suite is offline (stubs / MockTransport)
ruff check litcli tests                 # rules are pinned in pyproject.toml
scripts/release.sh --check              # release gates (--dry-run / --bump / --push: see AGENTS.md)
```

`release.sh` preflights `ruff` and `build` (and prints that install line when either is missing); a `--binary`
release additionally needs the [GitHub CLI](https://cli.github.com) (`gh`) to fetch the other platform's installer.

CI runs on the GitHub mirror (`master` and `v*` tags: lint, tests, packaging); releases are cut from the
Gitee repo and verified against the published assets afterwards. Changes are listed in
[`CHANGELOG.md`](CHANGELOG.md).
