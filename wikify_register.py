#!/usr/bin/env python3
"""journal-toc Step 2e — wikify every downloaded article PDF + register in PG by DOI.

The NEJM TOC workflow must routinely (1) MinerU-wikify ALL per-article
source.pdf → raw.md bundles, and (2) register each in PG
`wiki_raw.raw_source_metadata` keyed by DOI. Previously the cron only built
the mega-md + downloaded PDFs, leaving 0 per-article raw.md and 0 PG rows.

Per article (inbox file `<journal>-<date>_<ckey>_source.pdf`):
  - bundle = <corpus-issue-dir>/<ckey>/
  - if bundle/raw.md missing → MinerU (`-m auto -b pipeline -l en`) → raw.md
    (pdftotext banned) + images/. Frontmatter carries doi/citation_key.
  - write bundle/manifest.json (citation_key, doi, source_uid, title, …).
  - PG UPSERT: source_uid='doi_10.1056_<CitationKey>' (/ , : → _), doi,
    citation_key, source_type=journal-article, title (from mega), paths,
    mineru_status=done, ingest_status=promoted. ON CONFLICT DO NOTHING; a DOI
    already registered under another uid is reported DUP, not re-inserted.

Default dry-run (emits SQL to /tmp). --apply runs psql against the configured
PG (PGHOST/PGUSER/PGDATABASE). Idempotent.

Usage:
  wikify_register.py --journal nejm --date 2026-06-11 [--apply] [--no-mineru]
"""
import argparse, atexit, json, os, re, shutil, subprocess, sys, datetime
from pathlib import Path
from journal_paths import issue_dir as canonical_issue_dir, corpus_root

MINERU = os.environ.get("MINERU_BIN", "mineru")
HOME = Path.home()
HERE = Path(__file__).resolve().parent
# OA full-text fetcher (unpaywall/PMC, no cookies). Used only in --oa-fetch mode.
# Point JOURNAL_FETCH_SCRIPT at your own by-DOI fetcher; blank disables --oa-fetch.
JOURNAL_FETCH = os.environ.get("JOURNAL_FETCH_SCRIPT", "")

# --- journal identity, journals.json-derived (was NEJM-only hardcode) ----------
# Pre-generalisation these were NEJM-only literals; generalised so the weekday
# journals (aim/jama/nature/science/lancet/bmj) register under their real DOI.
# NEJM behaviour is unchanged (falls through to the same name/prefix).
_NEJM_NAME = "New England Journal of Medicine"
_NEJM_PREFIX = "10.1056"


def _psql_base():
    """psql connection args from the environment (PGHOST/PGUSER/PGDATABASE)."""
    return [
        os.environ.get("PSQL_BIN", "psql"),
        "-h", os.environ.get("PGHOST", "localhost"),
        "-U", os.environ.get("PGUSER", "postgres"),
        "-d", os.environ.get("PGDATABASE", "journal_bundler"),
    ]


def _load_journals_cfg():
    try:
        return json.loads((HERE / "journals.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


_JCFG = _load_journals_cfg()


def journal_name(journal):
    c = _JCFG.get(journal, {})
    return c.get("name") or (_NEJM_NAME if journal == "nejm" else journal.upper())


def journal_doi_prefix(journal):
    c = _JCFG.get(journal, {})
    # journals.json stores doi_prefix WITH a trailing slash ("10.1056/"); the
    # PG/uid scheme here wants it WITHOUT.
    p = (c.get("doi_prefix") or (_NEJM_PREFIX if journal == "nejm" else "")).rstrip("/")
    return p


# Back-compat shims: a few call sites still index these like the old dicts.
class _JLookup:
    def __init__(self, fn):
        self._fn = fn

    def __getitem__(self, j):
        return self._fn(j)

    def get(self, j, default=None):
        v = self._fn(j)
        return v if v else default


JOURNAL_NAME = _JLookup(journal_name)
DOI_PREFIX = _JLookup(journal_doi_prefix)

_drain_paused = False
DRAIN_JOB = os.environ.get("MINERU_DRAIN_JOB", "")


def ensure_drain_paused():
    """Pause the MinerU drain worker so Step 2e's MinerU isn't starved for GPU.
    Best-effort + idempotent; auto-resumed via atexit. No-op when
    MINERU_DRAIN_JOB is unset or the launchd job is absent (macOS launchctl)."""
    global _drain_paused
    if _drain_paused:
        return
    if not DRAIN_JOB:
        return  # no drain worker configured
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{DRAIN_JOB}"],
        capture_output=True,
    )
    # Do not pkill a live MinerU child. Every extraction entrypoint already
    # shares /tmp/mineru.global.lock, so the first journal extraction waits for
    # the current child to finish. Killing it corrupts an unrelated corpus job
    # and lets the still-running drain worker immediately race for the lock.
    _drain_paused = result.returncode == 0


def drain_resume():
    if not _drain_paused:
        return
    plist = HOME / "Library/LaunchAgents" / f"{DRAIN_JOB}.plist"
    if plist.exists():
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                       capture_output=True)


def sqlstr(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def fs_safe(s):
    return s.replace(":", "_").replace("/", "_")


def citation_key_from_ckey(ckey, journal):
    # nejmoa2600440 -> NEJMoa2600440 (journal prefix upper-cased)
    if journal == "nejm" and ckey.lower().startswith("nejm"):
        return "NEJM" + ckey[4:]
    return ckey


def load_mega_titles(issue_dir, journal, date):
    """doi-suffix(lower) -> title, from the mega-md article sections."""
    mega = issue_dir / "mega.md"
    if not mega.is_file():
        mega = issue_dir / f"{journal}-{date}.md"  # pre-migration fallback
    out = {}
    if not mega.is_file():
        return out
    text = mega.read_text(encoding="utf-8", errors="replace")
    for chunk in re.split(r"\n##\s+Article\s+\d+\s+—\s+", text)[1:]:
        title = chunk.split("\n", 1)[0].strip()
        m = re.search(r"\*\*DOI\*\*:\s*\[([^\]]+)\]", chunk)
        if m:
            out[m.group(1).strip().split("/")[-1].lower()] = title
    return out


def parse_mega_oa_articles(issue_dir, journal, date):
    """Return [{doi, ckey, title, oa}] for every article in the mega-md.

    The mega-md (built by bundler.py) carries a real per-article DOI + an
    `**OA**: yes|no` flag — the authoritative source of identity + OA status for
    the weekday journals (whose DOI is NOT derivable from the citation key the
    way NEJM's is). Used by --oa-fetch to pick which articles to full-text.
    """
    mega = issue_dir / "mega.md"
    if not mega.is_file():
        mega = issue_dir / f"{journal}-{date}.md"  # pre-migration fallback
    out = []
    if not mega.is_file():
        return out
    text = mega.read_text(encoding="utf-8", errors="replace")
    for chunk in re.split(r"\n##\s+Article\s+\d+\s+—\s+", text)[1:]:
        title = chunk.split("\n", 1)[0].strip()
        md = re.search(r"\*\*DOI\*\*:\s*\[([^\]]+)\]", chunk)
        if not md:
            continue
        doi = md.group(1).strip()
        # OA field values: yes (publisher-page free) | green (repository-only
        # free copy — publisher page paywalled) | no. Both yes and green have
        # fetchable free full text (green via unpaywall/PMC), so both qualify
        # for --oa-fetch.
        oam = re.search(r"\*\*OA\*\*:\s*(yes|green|no)", chunk, re.I)
        oa = bool(oam) and oam.group(1).lower() in ("yes", "green")
        pum = re.search(r"\*\*PDF URL\*\*:\s*<?([^>\s]+)>?", chunk)
        pdf_url = pum.group(1).strip() if pum else ""
        out.append({"doi": doi, "ckey": doi.split("/", 1)[-1], "title": title,
                    "oa": oa, "pdf_url": pdf_url})
    return out


# Publishers whose gold-OA article PDFs are fetchable by a plain HTTPS GET (no
# bot-block, no cookies, no proxy). JAMA/AIM/NEJM 403 a plain curl of their PDF
# path, so they are NOT here — their OA full text arrives via unpaywall->PMC
# (with a lag that self-heals on the next idempotent lane re-run). Add a host
# only after verifying `curl -IL` returns 200 application/pdf without cookies.
PUBLISHER_DIRECT_OA_HOSTS = ("www.bmj.com",)


def _pdf_host_allowed(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname in PUBLISHER_DIRECT_OA_HOSTS
    except Exception:
        return False


def publisher_direct_oa_fetch(url, dest):
    """Fetch a gold-OA article PDF directly from the publisher site (public, no cookies /
    no proxy). unpaywall/PMC lag fresh BMJ issues; the OA:yes PDF URL carried in
    the mega-md is freely served for Research (gold-OA) articles. Verifies %PDF
    magic + size so a paywall/login HTML page is rejected. This is the publisher's
    own free URL, NOT a paywall/library proxy (no proxy autofetch).
    """
    try:
        subprocess.run(
            [shutil.which("curl") or "curl", "-sL", "-A",
             "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             "--max-time", "150", url, "-o", str(dest)],
            capture_output=True, text=True, timeout=180)
    except Exception:
        return False
    if dest.exists() and dest.stat().st_size > 2000 and dest.read_bytes()[:5] == b"%PDF-":
        return True
    if dest.exists():
        dest.unlink()
    return False


def oa_fetch_to_inbox(issue_dir, journal, date, existing_dois, output_dir=None):
    """For each OA article not already in PG, fetch the OA full-text PDF via
    JOURNAL_FETCH_SCRIPT (unpaywall → PMC, NO cookies / NO proxy) and drop it in
    the inbox as `<journal>-<date>_<ckey>_source.pdf` so the normal PDF→MinerU→
    register loop ingests it. Non-OA articles are left abstract-only (paywalled
    full-text needs a separately-configured proxy path).

    Best-effort + non-blocking: a fresh issue's OA articles are often not yet
    indexed by unpaywall/PMC (404) — those are skipped this run and picked up on
    a later idempotent re-run. Returns (fetched, skipped, already) counts.
    """
    arts = parse_mega_oa_articles(issue_dir, journal, date)
    output_dir = output_dir or issue_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    oa_arts = [a for a in arts if a["oa"]]
    fetched = skipped = already = 0
    if not oa_arts:
        print(f"[oa-fetch] {journal} {date}: 0 OA-flagged articles in mega-md")
        return (0, 0, 0)
    if not JOURNAL_FETCH:
        print("[oa-fetch] SKIP — set JOURNAL_FETCH_SCRIPT to a by-DOI OA "
              "full-text fetcher to use --oa-fetch")
        return (0, len(oa_arts), 0)
    if not Path(JOURNAL_FETCH).is_file():
        print(f"[oa-fetch] SKIP — fetcher missing: {JOURNAL_FETCH}")
        return (0, len(oa_arts), 0)
    print(f"[oa-fetch] {journal} {date}: {len(oa_arts)} OA-flagged article(s)")
    for a in oa_arts:
        doi = a["doi"]
        ckey = a["ckey"]
        dest = output_dir / f"{journal}-{date}_{ckey}_source.pdf"
        if dest.exists() and dest.stat().st_size > 0:
            already += 1
            continue
        if doi.lower() in existing_dois:
            # full text already registered; no need to re-fetch
            already += 1
            continue
        scratch = Path("/tmp") / f"oa-fetch-{journal}-{ckey}"
        if scratch.exists():
            subprocess.run(["rm", "-rf", str(scratch)])
        _timeout = shutil.which("gtimeout") or shutil.which("timeout")
        r = subprocess.run(
            ([_timeout, "180"] if _timeout else [])
            + [sys.executable, str(JOURNAL_FETCH), doi,
               "--out-dir", str(scratch), "--json"],
            capture_output=True, text=True)
        status = ""
        try:
            status = (json.loads(r.stdout.strip().splitlines()[-1])
                      if r.stdout.strip() else {}).get("status", "")
        except Exception:
            status = f"rc={r.returncode}"
        # locate the fetched source.pdf anywhere under scratch (slug subdir)
        pdf = None
        if scratch.exists():
            for cand in scratch.rglob("source.pdf"):
                if cand.stat().st_size > 0:
                    pdf = cand
                    break
        if pdf:
            subprocess.run(["cp", str(pdf), str(dest)], check=True)
            fetched += 1
            print(f"  [oa-fetch OK] {ckey} ({status}) -> {dest.name}")
        elif a.get("pdf_url") and _pdf_host_allowed(a["pdf_url"]) \
                and publisher_direct_oa_fetch(a["pdf_url"], dest):
            fetched += 1
            print(f"  [oa-fetch OK] {ckey} ({status or 'needs_proxy'}+publisher-direct) -> {dest.name}")
        else:
            skipped += 1
            print(f"  [oa-fetch skip] {ckey}: {status or 'no OA pdf (not yet indexed?)'}")
        if scratch.exists():
            subprocess.run(["rm", "-rf", str(scratch)])
    print(f"[oa-fetch] done: fetched={fetched} skipped={skipped} already={already}")
    return (fetched, skipped, already)


def pg_existing_dois():
    sql = ("SELECT lower(doi) FROM wiki_raw.raw_source_metadata "
           "WHERE doi IS NOT NULL AND sync_deleted_at IS NULL")
    r = subprocess.run(_psql_base() + ["-tAc", sql], capture_output=True, text=True)
    return {x.strip() for x in r.stdout.splitlines() if x.strip()}


def run_mineru(pdf: Path, bundle: Path, fm: str):
    work = Path("/tmp") / f"wikifyreg-{bundle.name}"
    if work.exists():
        subprocess.run(["rm", "-rf", str(work)])
    work.mkdir(parents=True)
    subprocess.run(["cp", str(pdf), str(work / "source.pdf")], check=True)
    ensure_drain_paused()   # free the GPU before the first MinerU run
    failure_log = pdf.with_name(f".{pdf.stem}.mineru.log")
    with failure_log.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [MINERU, "-p", "source.pdf", "-o", "./", "-m", "auto",
             "-b", "pipeline", "-l", "en"],
            cwd=work,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    md = work / "source" / "auto" / "source.md"
    if not md.is_file() or md.stat().st_size == 0:
        print(f"  [mineru FAIL] {pdf.name}: rc={result.returncode} log={failure_log}",
              file=sys.stderr)
        subprocess.run(["rm", "-rf", str(work)])
        return False
    failure_log.unlink(missing_ok=True)
    bundle.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", str(pdf), str(bundle / "source.pdf")], check=True)
    (bundle / "raw.md").write_text(fm + md.read_text(encoding="utf-8"), encoding="utf-8")
    imgs = work / "source" / "auto" / "images"
    if imgs.is_dir():
        (bundle / "images").mkdir(exist_ok=True)
        for im in imgs.iterdir():
            subprocess.run(["cp", str(im), str(bundle / "images" / im.name)])
    subprocess.run(["rm", "-rf", str(work)])
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="nejm")
    ap.add_argument("--date", required=True, help="issue date YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-mineru", action="store_true",
                    help="skip MinerU; only manifest + register existing raw.md")
    ap.add_argument("--oa-fetch", action="store_true",
                    help="before wikify, fetch OA full-text for each mega-md "
                         "**OA**:yes article (unpaywall/PMC, no cookies) into the "
                         "inbox so it gets MinerU+registered. Non-OA stay "
                         "abstract-only. Best-effort; fresh-issue OA not yet "
                         "indexed is skipped (idempotent re-run picks it up).")
    args = ap.parse_args()
    atexit.register(drain_resume)   # always restore the drain worker on exit
    j, date = args.journal, args.date
    year = date[:4]
    inbox = Path(os.environ.get("JOURNAL_INBOX", str(HOME / "Downloads"))) \
        / "journal-toc" / j / date
    issue_source = canonical_issue_dir(j, date)
    corpus = corpus_root() / j.upper() / year / date
    titles = load_mega_titles(issue_source, j, date)
    existing = pg_existing_dois()

    # OA-conditional full-text fetch (OA-gated — NOT whole-issue ingest, which
    # the pipeline's open-access guard forbids). Pulls only OA full-text into
    # the inbox; the loop below ingests it.
    if args.oa_fetch:
        oa_fetch_to_inbox(issue_source, j, date, existing, output_dir=inbox)

    pdfs = sorted(inbox.glob(f"{j}-{date}_*_source.pdf"))
    rows, dup, fail = [], [], []
    for pdf in pdfs:
        ckey = pdf.name[len(f"{j}-{date}_"):-len("_source.pdf")]
        ck = citation_key_from_ckey(ckey, j)
        doi = f"{DOI_PREFIX[j]}/{ckey}"            # lowercase, matches PG doi col
        source_uid = fs_safe(f"doi_{DOI_PREFIX[j]}/{ck}")  # doi_10.1056_NEJMoa…
        title = titles.get(ckey.lower(), ck)
        bundle = corpus / ckey
        raw = bundle / "raw.md"
        fm = (f"---\ntype: raw\nsource_type: journal-article\n"
              f"citation_key: {ck}\nuid: \"doi:{DOI_PREFIX[j]}/{ck}\"\n"
              f"doi: \"{DOI_PREFIX[j]}/{ck}\"\njournal: {JOURNAL_NAME[j]}\n"
              f"issue_date: {date}\nextracted_by: mineru-pipeline\n"
              f"generated: {date}\n---\n\n")
        if not raw.is_file():
            if args.no_mineru:
                fail.append((ckey, "no raw.md (--no-mineru)")); continue
            if not run_mineru(pdf, bundle, fm):
                fail.append((ckey, "mineru-failed")); continue
        # manifest.json (idempotent overwrite)
        bundle.mkdir(parents=True, exist_ok=True)
        manifest = {
            "type": "journal_article", "source_type": "journal-article",
            "source_uid": source_uid, "citation_key": ck,
            "doi": f"{DOI_PREFIX[j]}/{ck}", "journal": JOURNAL_NAME[j],
            "year": int(year), "issue_date": date, "title": title,
            "source_metadata": {"citation_key": ck, "source_type": "journal-article",
                                "doi": f"{DOI_PREFIX[j]}/{ck}", "title": title},
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        if doi.lower() in existing:
            dup.append((ckey, doi)); continue
        rows.append({"source_uid": source_uid, "ck": ck, "doi": doi,
                     "title": title, "year": year, "journal": JOURNAL_NAME[j],
                     "efp": str(bundle), "raw": str(raw),
                     "pdf": str(bundle / "source.pdf")})

    flag = f"wikify-register-{date}"
    lines = ["BEGIN;"]
    for r in rows:
        lines.append(
            "INSERT INTO wiki_raw.raw_source_metadata (source_uid, citation_key, "
            "source_type, sidecar_key, title, doi, year, journal, end_folder_path, raw_md_path, "
            "source_pdf_path, mineru_status, ingest_status, payload_flags) VALUES ("
            f"{sqlstr(r['source_uid'])}, {sqlstr(r['ck'])}, 'journal-article', {sqlstr(r['ck'])}, "
            f"{sqlstr(r['title'])}, {sqlstr(r['doi'])}, {r['year']}, {sqlstr(r['journal'])}, "
            f"{sqlstr(r['efp'])}, {sqlstr(r['raw'])}, {sqlstr(r['pdf'])}, "
            f"'done', 'promoted', ARRAY[{sqlstr(flag)}]::text[]) "
            "ON CONFLICT DO NOTHING;")
    lines.append("COMMIT;")
    sqlf = Path("/tmp") / f"wikify-register-{j}-{date}.sql"
    sqlf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"PDFs: {len(pdfs)} | NEW rows: {len(rows)} | DUP (doi already in PG): "
          f"{len(dup)} | FAIL: {len(fail)}")
    for c, why in fail:
        print(f"  FAIL {c}: {why}")
    print(f"SQL: {sqlf}")
    if not args.apply:
        print("DRY-RUN — re-run with --apply to register in PG.")
        return
    if rows:
        res = subprocess.run(
            _psql_base() + ["-v", "ON_ERROR_STOP=1", "-f", str(sqlf)],
            capture_output=True, text=True)
        sys.stdout.write(res.stdout); sys.stderr.write(res.stderr)
        if res.returncode != 0:
            sys.exit("APPLY failed")
        print(f"applied: {len(rows)} INSERT(s)")


if __name__ == "__main__":
    main()
