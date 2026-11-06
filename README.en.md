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
  handed back to you, and results from shadow libraries are labelled as non-OA.

## Quick start (no Zotero required)

```bash
python3 -m venv .venv && . .venv/bin/activate                    # or use an environment you have
pip install .                                                    # Debian/Ubuntu PEP 668: use ./install.sh
litcli search "graph neural network" --limit 5            # every source by default; the key is the DOI
litcli data get 10.1038/s41598-024-80509-9 --backend crossref --format json
litcli export 10.1038/s41598-024-80509-9 --backend crossref --format csljson
litcli fetch 10.1038/s41598-024-80509-9            # saved to the current directory; stdout is the path
```

`search` covers every source by default; `--backend` narrows it. `--format json` prints one record per line and an
empty result prints `[]`. `fetch` writes into the current directory (`--out DIR` overrides), prints only the path
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
pip install .                 # into the current environment (venv, Python 3.11+)
./install.sh                  # Debian/Ubuntu PEP 668: --user snapshot into ~/.local
litcli backends               # self-test: configured backends and their capabilities
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
elsewhere). Upgrading means rerunning `./install.sh` (it overwrites the snapshot); uninstall with `pip uninstall litcli` and
remove `~/.config/litcli` and `~/.local/share/litcli`.

Everything stays local: citations go into your own Zotero library, config into
`~/.config/litcli/config.toml`, and the only outbound traffic is to the upstream APIs. `install.sh`
also distributes `skills/litcli/SKILL.md` to any detected agent (Claude Code / Codex / Cursor /
Gemini) and keeps a neutral copy under `~/.local/share/litcli/skill/`.

## Commands

`--backend` picks the source (default `zotero`); `--all` aggregates across the backends **enabled in your config**,
de-duplicates and tags `extra["sources"]`. The second column of a result row is the key; for Zotero drop the
`source/` prefix, public backends accept either form. Exit codes: `0` success (including no results), `2` error or
unsupported backend. `search`, `fetch`, `export`, `refs`, `citing`, `related` are read-only;
`data` writes.

**Read-only**

| Command | What it does |
|---|---|
| `litcli search "X" [--all] [--backend B] [--type T] [--limit N] [--format table\|json\|csl]` | candidate search; field filters and sorting below |
| `litcli refs <key>` / `citing <key>` / `related <key>` | cited works / citing works / semantic neighbours (OpenAlex graph) |
| `litcli export <key>... [--format csljson\|bibtex\|ris\|text] [--no-cite]` | citations; `csljson` feeds `pandoc --citeproc`. arXiv keys normalise to a DOI only with `--backend openalex\|crossref` |
| `litcli fetch <doi\|arXiv id> [--out DIR] [--no-download] [--scihub]` | save the fulltext: local store → Unpaywall → OpenAlex → Europe PMC → Zenodo → landing page |
| `litcli fetch --book "title\|ISBN" [--backend zlibrary\|doab\|openlibrary] [--pick N]` | book candidates → pick → full metadata → save the file (an ISBN takes this path automatically) |
| `litcli backends authorize zotero` | one-off write authorization (local dialog) |
| `litcli data add --title "..." --creator "Family, Given" [--doi ..] [--force]` / `data add --pdf|--csl <file>` | ingest metadata (dedupe gate, `--force` skips), heuristics from page 1 (y/N), or CSL JSON |
| `litcli data get <key>` · `data tree` · `data members <coll> [--limit N]` | read one record; browse collections |
| `litcli backends list/status` · `litcli completions <bash\|zsh\|fish>` | diagnostics, shell completion |

**Writes** (Zotero only; read-only backends answer `[skipped]` and exit 2)

| Command | What it does |
|---|---|
| `litcli backends authorize zotero` | one-off write authorization (local dialog) |
| `litcli data add ...` | ingest (above) |
| `litcli data set <key> --title ..` · `data del <key>` · `data attach <key> <path\|URL>` | edit, delete, attach a file |

## Backends and what they may fetch

Zotero, arXiv, Crossref and OpenAlex are enabled by default. Every other backend works with a one-off `--backend`
without any config; you only add it to `config.toml` to include it in `--all` or the default `fetch` chain.

| Backend | Access | What is fetched |
|---|---|---|
| `zotero` | read / write / attachment — your local library | local API, one write authorization |
| `arxiv` · `crossref` · `openalex` | free, no key | metadata (arxiv also PDF links); rate-limited and backed off |
| `doaj` · `europepmc` · `doab` · `zenodo` | open access, no key | public OA records (JATS XML / open files) |
| `openlibrary` · `uspto` | free (uspto needs a free key) | book records / patents |
| `cnki` | CNKI web index | public metadata only, never paywalled full text; visible browser + manual slider |
| `douban` | Douban web | public bibliographic data; headless |
| `scihub` | shadow library (non-OA), DOI only | PDF, opt-in via `--scihub` |
| `zlibrary` | shadow library (non-OA) | records + PDF; anonymous daily quota |

**China-specific sources.** CNKI drives a visible browser with your own logged-in profile and needs one
manual slider; Douban runs headless by default (its search page ships the records server-side) and only
drops to a visible browser if it flags your traffic. Neither is needed for the international sources.
A 403 from `europepmc.org` or DOAB means "no JS client", not "your IP is banned" — the CLI routes those
two around the wall; DOAJ has no wall at all.

## Configuration

`~/.config/litcli/config.toml` (`%APPDATA%\litcli\config.toml` on Windows; `$LITCLI_CONFIG`
overrides); with no file, a built-in default of Zotero + arXiv + Crossref + OpenAlex applies.
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
- The binary installer ships no Chromium: CNKI/Douban need `litcli backends install-browser` first.
- `api_key` and Z-Library credentials are stored in plain text; `backends authorize` chmods the config, hand-edited
  credentials are not covered. On Windows chmod only clears the read-only attribute — the `%APPDATA%`
  profile ACL is what isolates the file.

## Development

```bash
python3 -m unittest discover -s tests   # the whole suite is offline (stubs / MockTransport)
ruff check litcli tests
scripts/release.sh --check              # release gates (--dry-run / --bump / --push: see AGENTS.md)
```

CI runs on the GitHub mirror (`master` and `v*` tags: lint, tests, packaging); releases are cut from the
Gitee repo and verified against the published assets afterwards. Changes are listed in `CHANGELOG.md`.
