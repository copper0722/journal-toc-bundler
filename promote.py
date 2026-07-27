#!/usr/bin/env python3
"""promote.py — promote staged journal article bundles from
$JOURNAL_TOC_STAGING/_journal_toc/<j>/<date>/articles/<id>/ into the canonical
<corpus>/journal/<JournalDir>/<year>/<YYYY-MM-DD>/<id>/ scheme.

MOVES article bundles to the corpus (canonical home); the issue `mega.md` is
already written directly to the formal issue corpus folder. Manifest-less bundles get
a backfilled manifest. `.dup-from-plus` duplicate dirs are skipped. Sets
column_tag where derivable (NEJM citation-key prefix). Idempotent.

Run on the local host. Usage: promote.py [--run]   (default dry-run)
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from journal_paths import corpus_root

HOME = Path.home()
STAGING_BASE = Path(os.environ.get(
    "JOURNAL_TOC_STAGING",
    str(HOME / "Downloads" / "journal-toc" / "_staging"))).expanduser()
TOC_ROOTS = [STAGING_BASE / "_journal_toc"]
JOURNAL = corpus_root()  # canonical journal corpus root
DRY = "--run" not in sys.argv

JDIR = {"nejm": "NEJM", "aim": "AIM", "bmj": "BMJ", "jama": "JAMA",
        "lancet": "Lancet", "science": "Science", "nature": "Nature"}
JFULL = {"nejm": "New England Journal of Medicine", "aim": "Annals of Internal Medicine",
         "bmj": "BMJ", "jama": "JAMA", "lancet": "The Lancet",
         "science": "Science", "nature": "Nature"}
NEJM_COL = [
    ("nejmcpc", "case_records"), ("nejmicm", "images_in_clinical_medicine"),
    ("nejmcps", "clinical_problem_solving"), ("nejmclde", "clinical_decisions"),
    ("nejmoa", "original_article"), ("nejmra", "review_article"),
    ("nejmcp", "clinical_practice"), ("nejme", "editorial"),
    ("nejmp", "perspective"), ("nejmc", "correspondence"),
]


def _psql_base():
    """psql connection args from the environment (PGHOST/PGUSER/PGDATABASE)."""
    return [
        os.environ.get("PSQL_BIN", "psql"),
        "-h", os.environ.get("PGHOST", "localhost"),
        "-U", os.environ.get("PGUSER", "postgres"),
        "-d", os.environ.get("PGDATABASE", "journal_bundler"),
    ]


def nejm_column_tag(ck):
    c = ck.lower()
    for pre, col in NEJM_COL:
        if c.startswith(pre):
            return f"nejm_{col}"
    return None



def _fs_safe(x):
    return x.replace(":", "_").replace("/", "_")


def doi_in_pg(doi):
    """True if `doi` is already registered under ANY source_uid (case-insensitive).

    Closes the case-variant-uid duplicate gap: the per-issue NEJM/AIM pipeline
    registers the same article under differing uid schemes
    (doi_10.1056_NEJMp.. vs doi_10_1056_nejmp.. vs source_nejmp..), so a
    source_uid-only ON CONFLICT does NOT dedup. Gate the promote on lower(doi)
    instead. Best-effort: on psql failure returns False (fail-open — a transient
    DB error must not silently skip a genuinely-new bundle; a same-uid
    ON CONFLICT in register_pg is the second line of defence).
    """
    if not doi:
        return False
    sql = ("SELECT 1 FROM wiki_raw.raw_source_metadata "
           "WHERE lower(doi) = lower(%s) AND sync_deleted_at IS NULL LIMIT 1")
    # psql can't bind %s without a driver here; inline-escape the literal.
    lit = "'" + str(doi).replace("'", "''") + "'"
    sql = sql.replace("%s", lit)
    r = subprocess.run(_psql_base() + ["-tAc", sql], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [pg] WARN doi-gate lookup failed (fail-open): "
              f"{r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'rc='+str(r.returncode)}")
        return False
    return bool(r.stdout.strip())


def register_pg(j, date, cid, d, target):
    """UPSERT the promoted bundle into PG wiki_raw.raw_source_metadata by DOI.

    Mirrors wikify_register.py step-2e shape, but HONEST about extraction:
    browser-body raw.md (no source.pdf / no MinerU run) -> mineru_status
    not_applicable; a real MinerU bundle (source.pdf present) -> done.
    oa_status from manifest is_oa. Idempotent: ON CONFLICT (source_uid) DO
    NOTHING. Best-effort; psql failure is non-fatal (WARN). source_uid =
    doi_<prefix>_<citation_key> with ':' '/' -> '_' (dots preserved, matching
    the NEJM rows).
    """
    doi = (d.get("doi") or "").strip()
    if not doi:
        print(f"    [pg] skip {cid}: no doi in manifest")
        return False
    source_uid = _fs_safe("doi_" + doi)
    ck = d.get("citation_key") or cid
    title = (d.get("title") or d.get("source_metadata", {}).get("title") or ck).strip()
    journal = d.get("journal") or JFULL.get(j, j.upper())
    year = d.get("year")
    if not isinstance(year, int):
        year = int(date[:4]) if date[:4].isdigit() else None
    has_pdf = (target / "source.pdf").exists()
    mineru_status = "done" if has_pdf else "not_applicable"
    oa = bool(d.get("is_oa"))
    oa_status = "positive" if oa else "unknown"
    efp = str(target)
    raw = str(target / "raw.md")
    flag = f"journal-toc-promote-{date}"

    def q(v):
        return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"

    cols = ("source_uid, citation_key, source_type, sidecar_key, title, doi, "
            "year, journal, end_folder_path, raw_md_path, mineru_status, "
            "ingest_status, oa_status, payload_flags")
    vals = (f"{q(source_uid)}, {q(ck)}, 'journal-article', {q(ck)}, {q(title)}, "
            f"{q(doi.lower())}, {year if year is not None else 'NULL'}, {q(journal)}, "
            f"{q(efp)}, {q(raw)}, {q(mineru_status)}, 'promoted', {q(oa_status)}, "
            f"ARRAY[{q(flag)}]::text[]")
    sql = (f"INSERT INTO wiki_raw.raw_source_metadata ({cols}) VALUES ({vals}) "
           "ON CONFLICT (source_uid) DO NOTHING;")
    r = subprocess.run(_psql_base() + ["-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [pg] WARN register {cid}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'rc='+str(r.returncode)}")
        return False
    print(f"    [pg] {r.stdout.strip()}  {source_uid} ({mineru_status})")
    return True


def main():
    staged = []
    for root in TOC_ROOTS:
        staged.extend((root, d) for d in root.glob("*/*/articles/*")
                      if d.is_dir() and not d.name.endswith(".dup-from-plus"))
    staged.sort(key=lambda item: str(item[1]))
    moved = skipped = backfilled = doi_skipped = would_promote = 0
    for root, b in staged:
        rel = b.relative_to(root).parts
        if len(rel) != 4 or rel[2] != "articles":
            print(f"  SKIP layout: {'/'.join(rel)}")
            continue
        j, date, _, cid = rel
        jdir = JDIR.get(j, j.upper())
        year = date[:4]
        target = JOURNAL / jdir / year / date / cid
        mf = b / "manifest.json"
        if mf.exists():
            d = json.loads(mf.read_text())
        else:
            d = {"type": "journal_article", "journal": JFULL.get(j, j.upper()),
                 "citation_key": cid,
                 "note": "manifest backfilled by journal-toc promotion"}
            backfilled += 1
        d["year"] = int(year) if year.isdigit() else year
        d["issue_id"] = date
        ctag = nejm_column_tag(cid) if j == "nejm" else d.get("column_tag")
        if ctag:
            d["column_tag"] = ctag
            tags = d.get("tags") or []
            if ctag not in tags:
                tags.append(ctag)
            d["tags"] = tags
        if target.exists():
            print(f"  SKIP(exists)  {'/'.join(rel)}")
            skipped += 1
            continue
        # DOI dedup gate: skip if this DOI is already registered under ANY
        # source_uid (case-variant uid schemes the per-issue pipeline produces,
        # which ON CONFLICT (source_uid) misses). Runs in dry-run too, so the
        # summary honestly reflects 0 would-promote.
        _doi = (d.get("doi") or "").strip()
        if not _doi:
            print(f"  SKIP(no-doi)  {'/'.join(rel)} — manifest has no DOI; not promoting")
            skipped += 1
            continue
        if doi_in_pg(_doi):
            print(f"  SKIP(doi-in-pg)  {'/'.join(rel)} — DOI {_doi} already registered under existing uid")
            doi_skipped += 1
            continue
        flag = "" if mf.exists() else " [manifest backfilled]"
        would_promote += 1
        print(f"  MOVE  {j}/{date}/{cid} -> {jdir}/{year}/{date}/{cid}  tag={ctag}{flag}")
        if DRY:
            continue
        b.joinpath("manifest.json").write_text(
            json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(b), str(target))
        moved += 1
        register_pg(j, date, cid, d, target)
    print(f"\n{'DRY-RUN (pass --run)' if DRY else 'DONE'}: {len(staged)} bundles — "
          f"would-promote={moved} skipped(exists)={skipped} "
          f"skipped(doi-in-pg)={doi_skipped} (manifest-backfilled={backfilled})")


if __name__ == "__main__":
    main()
