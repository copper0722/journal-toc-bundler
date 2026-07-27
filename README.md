# journal-toc-bundler

Turn a medical/scientific journal's **current-issue Table of Contents** into a
structured, RAG-ready knowledge bundle: one "mega" markdown file for the whole
issue plus optional per-article payload folders (raw markdown, manifest, source
PDF, and folded media transcripts).

It drives a **browser tab you are already logged into** (via AppleScript on
macOS) to read subscriber/paywalled article pages, and falls back to public
endpoints (Crossref, Unpaywall, publisher RSS) so open-access issues work with
no browser at all. Downstream helpers convert each PDF to markdown (MinerU),
transcribe article audio/video (whisper.cpp), promote bundles into a canonical
corpus tree, and register them in PostgreSQL.

> No credentials, cookies, tokens, or account details are shipped. Everything
> environment-specific is read from environment variables (see
> [`.env.example`](.env.example)). Gated journals are read through *your own*
> logged-in browser session; open-access journals need nothing.

## What it produces

For an issue of journal `<j>` dated `<YYYY-MM-DD>`:

```
$JOURNAL_CORPUS_ROOT/<JournalDir>/<YYYY>/<YYYY-MM-DD>/
├── mega.md                     # whole-issue "raw of raw": every article,
│                               #   section, type, DOI, abstract (+ full text
│                               #   / transcript where fetched)
└── <citation_key>/             # per-article bundle (when wiki_ingest_all=true
    ├── raw.md                  #   or --force-ingest-all)
    ├── manifest.json
    ├── source.pdf              # if fetch_pdf=true
    └── images/                 # MinerU-extracted figures
```

## Pipeline

| Step | Script | What it does |
|---|---|---|
| 1 | `bundler.py` | Parse the TOC (browser DOM or server-side RSS/Crossref), extract each article (title/abstract/body/DOI/figures), fetch PDFs + media, write `mega.md` and per-article bundles into staging. |
| 2a | `crossref_enrich.py` | Fill any missing abstract by DOI from the public Crossref API (no browser). Makes the harvest deterministic even from a cold tab. |
| 2d | `fold_transcripts.py` | ASR-transcribe fetched article audio/video (whisper.cpp) and fold the transcript into the issue `mega.md`. |
| 2e | `wikify_register.py` | MinerU-convert each article `source.pdf` to `raw.md`, then register the article in PostgreSQL keyed by DOI. |
| — | `fold_media_to_corpus.py` | Persist per-article media into the corpus bundles and record it in PG. |
| — | `podcast_bundler.py` | Detect publisher podcast/audio-player links in article bodies and bundle their transcripts. |
| — | `nejm-mega-fulltext.py` | Replace each article body in a `mega.md` with the verbatim full text from its `raw.md`/PDF (for journals where the web extract is only a summary). |
| — | `nejm_online_first_watch.py` | Poll an "online-first / recently-published" listing and notify once per newly-seen DOI (stdout / Telegram / email). |
| 3 | `promote.py` | Move finished bundles from staging into `$JOURNAL_CORPUS_ROOT` and backfill manifests. |
| — | `strip_metadata_appendix.py` | Strip the machine-only structured-metadata appendix from published index pages. |

Support modules: `journals.json` (per-journal config), `extractor.js` (the
in-page DOM + synchronous-XHR extraction layer), `run_in_browser.applescript`
(the browser bridge), `journal_paths.py` (canonical path helpers).

## Quick start

```bash
git clone <this repo> && cd journal-toc-bundler
cp .env.example .env          # then edit .env for your setup
# (recommended) export the vars you set:
set -a; . ./.env; set +a

# Open-access issue, no browser needed:
python3 bundler.py science --toc-only

# A journal whose article pages need your subscriber session:
#   1. Open the journal's current-issue TOC in Chrome/Safari and sign in.
#   2. Leave that tab open, then:
python3 bundler.py nejm

# Useful flags:
python3 bundler.py nejm --limit 3        # smoke test: first 3 articles
python3 bundler.py aim  --issue-date 2026-05-05
python3 bundler.py nejm --no-pdf         # mega.md only, skip PDF binaries
python3 bundler.py nejm --dois 10.1056/NEJMoa2515704 --force-ingest-all
```

`bundler.py <journal>` is the entry point; run `python3 bundler.py --help` for
the full flag list. The journal key is any top-level key in `journals.json`
(`aim`, `nejm`, `jama`, `nature`, `science`, `lancet`, `bmj`, `jasn`, `cjasn`,
`drugs`, `jfda`, `ki`, `ajkd`, `ndt`, `kidney360`, …).

## External stack it expects

Nothing below is bundled; each is optional and the tool degrades gracefully
when a piece is missing.

- **A logged-in browser (macOS)** — Chrome (any channel) or Safari, driven by
  `run_in_browser.applescript`. Only needed for journals whose full text sits
  behind a subscriber/paywall session. You sign in yourself; the tool never
  types or stores credentials. Open-access journals use public RSS/Crossref and
  need no browser (`--toc-only`).
- **PostgreSQL** — for dedup + article registration. Connection comes from
  `PGHOST`/`PGPORT`/`PGUSER`/`PGDATABASE` (or `JOURNAL_PG_DSN`). This is the
  schema the tool expects; create it yourself if you want the PG features:
  - `wiki_raw.raw_source_metadata` — one row per source (DOI, citation key,
    source type, title, `raw_md_path`, identifiers JSONB, `mineru_status`,
    `ingest_status`, `sync_deleted_at`, `updated_at`). The registration + dedup
    target.
  - `source_corpus.source_media` — per-article media rows.
  - `medical_knowledge.mesh_descriptor` + `medical_knowledge.tag` — optional
    MeSH auto-tagging (`--register-mesh`).
  - `admin_ops.nejm_online_first_seen` — state table for the online-first
    watcher (`doi` PK, title, url, pub_type, first_seen, notified_at).

  Schema/table names are what this codebase references; treat them as the
  tool's expected layout, not a universal standard. If PG is unreachable the
  bundler simply skips dedup/registration and still writes the corpus files.
- **MinerU** (`MINERU_BIN`) — PDF → markdown with tables, figures, multi-column
  reflow. Needed for per-article `raw.md`. https://github.com/opendatalab/MinerU
- **whisper.cpp** (`WHISPER_CLI` + `WHISPER_MODEL`) + **ffmpeg** (`FFMPEG_BIN`)
  — optional media ASR for transcript folding.

## Configuration

All environment variables and their defaults are documented in
[`.env.example`](.env.example). Highlights:

| Variable | Purpose | Default |
|---|---|---|
| `PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` | PostgreSQL connection | `localhost` / `5432` / `postgres` / `journal_bundler` |
| `JOURNAL_PG_DSN` | libpq DSN for psycopg2 features | assembled from the parts above |
| `JOURNAL_CORPUS_ROOT` | canonical corpus tree | `~/journal-corpus/journal` |
| `JOURNAL_PODCAST_ROOT` | podcast bundle tree | `~/journal-corpus/podcast` |
| `JOURNAL_TOC_STAGING` | pre-promotion staging dir | `~/Downloads/journal-toc/_staging` |
| `JOURNAL_INBOX` | where fetched binaries first land | `~/Downloads` |
| `JOURNAL_TOPIC_STORE_ROOT` | auto-routed article binary store | `~/journal-corpus/_topics` |
| `JOURNAL_TOPIC_STORE_HOST` | ssh host for a remote topic store | *(blank = local)* |
| `MINERU_BIN` / `WHISPER_CLI` / `WHISPER_MODEL` / `FFMPEG_BIN` | external tools | tool name on `PATH` |
| `CROSSREF_MAILTO` | Crossref/Unpaywall polite-pool contact | placeholder — **set your own** |
| `MINERU_DRAIN_JOB`, `JOURNAL_FETCH_SCRIPT`, `JOURNAL_TG_NOTIFY_SCRIPT`, `JOURNAL_GMAIL_ENV`, `MESH_HELPER_DIR` | optional integrations | *(blank = disabled)* |

### `journals.json`

Each key defines one journal: display `name`, `browser` app name, `toc_url`,
`doi_prefix`, `fulltext_url` / `pdf_url` templates, the CSS `*_selectors` used
by `extractor.js`, and behaviour flags (`fetch_pdf`, `fetch_audio`,
`wiki_ingest_all`, `toc_parser`, …). Add a journal by copying the closest
existing entry (same publishing platform) and adjusting the URLs + selectors.
Open-access journals can set a server-side `toc_format`/`toc_parser` and skip
the browser entirely.

## Legal / etiquette

This tool reads content **you already have lawful access to** — open-access
articles, or subscriber content through your own authenticated browser session.
It does not bypass authentication and ships no credentials. Respect each
publisher's terms of service and robots policy, keep request rates polite
(`--per-article-sleep` throttles bursty publishers), and set `CROSSREF_MAILTO`
to your own address so Crossref/Unpaywall can contact you. You are responsible
for how you use it.

## License

MIT — see [LICENSE](LICENSE). Author: copper0722.
