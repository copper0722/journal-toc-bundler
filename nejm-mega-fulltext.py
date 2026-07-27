#!/usr/bin/env python3
"""nejm-mega-fulltext.py — enforce the NEJM full-text mega protocol.

The NEJM issue mega-md MUST carry the VERBATIM COMPLETE FULL TEXT of every
article, not the web-extract summary (extractor.js yields methods + a
*summarized* Results ending "Full trial results are available at NEJM.org").
This pass rewrites each article's `### Body` with the real full text.

Full-text source per article, in order:
  1. corpus bundle raw.md (MinerU from source.pdf), located via PG DOI lookup
  2. else MinerU the issue's staging `*_<doi-suffix>_source.pdf`
Leaves Body untouched (and flags) if neither yields >1500 chars.

Idempotent: writes a one-time `<mega>.prefulltext.bak`; marks replaced bodies
with an HTML comment so re-runs are detectable. Reads/prefers corpus raw.md so a
second run after the daily pipeline finishes backfilling is cheap.

Usage:
  nejm-mega-fulltext.py <issue_dir>            # dry-run (report per-article source)
  nejm-mega-fulltext.py <issue_dir> --write    # rewrite the mega in place
"""
from __future__ import annotations
import argparse, glob, os, re, subprocess, sys, tempfile

HOME = os.path.expanduser("~")
MINERU = os.environ.get("MINERU_BIN", "mineru")
PG_HOST = os.environ.get("PGHOST", "localhost")
PG_USER = os.environ.get("PGUSER", "postgres")
PG_DB = os.environ.get("PGDATABASE", "journal_bundler")
MARK = "<!-- fulltext:mega-fulltext -->"

def log(m): print(m, flush=True)

def strip_frontmatter(t: str) -> str:
    if t.startswith("---\n"):
        end = t.find("\n---", 4)
        if end != -1:
            t = t[end + 4:]
    return t.lstrip("\n")

def pg_rawmd(doi: str) -> str | None:
    sql = ("select raw_md_path from wiki_raw.raw_source_metadata where doi=lower('%s') "
           "and raw_md_path is not null and sync_deleted_at is null "
           "order by updated_at desc nulls last limit 1;" % doi.replace("'", "''"))
    try:
        r = subprocess.run(["psql", "-h", PG_HOST, "-U", PG_USER, "-d", PG_DB,
                            "-tA", "-c", sql], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            p = r.stdout.strip().splitlines()
            if p and p[0].strip():
                return p[0].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None

def mineru_pdf(pdf: str) -> str | None:
    out = tempfile.mkdtemp(prefix="megaft-")
    try:
        subprocess.run([MINERU, "-p", pdf, "-o", out, "-b", "pipeline", "-m", "auto", "-l", "en"],
                       capture_output=True, text=True, timeout=900)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f"    mineru error: {e}"); return None
    mds = sorted(glob.glob(os.path.join(out, "**", "*.md"), recursive=True),
                 key=lambda p: os.path.getsize(p), reverse=True)
    if not mds:
        return None
    return open(mds[0], encoding="utf-8", errors="ignore").read()

def fulltext(doi: str, issue_dir: str) -> tuple[str | None, str]:
    raw = pg_rawmd(doi)
    if raw and os.path.exists(raw):
        t = strip_frontmatter(open(raw, encoding="utf-8", errors="ignore").read()).strip()
        if len(t) > 1500:
            return t, f"corpus-raw.md ({os.path.relpath(raw, HOME)})"
    suf = doi.split("/")[-1].lower()
    pdfs = glob.glob(os.path.join(issue_dir, f"*{suf}*source.pdf"))
    if pdfs:
        log(f"    MinerU {os.path.basename(pdfs[0])} ...")
        t = mineru_pdf(pdfs[0])
        if t and len(t) > 1500:
            return t.strip(), "mineru-staging-pdf"
    return None, "NONE"

ART_SPLIT = re.compile(r"(?=^## Article \d+ — )", re.M)
DOI_RE = re.compile(r"^- \*\*DOI\*\*: \[([^\]]+)\]", re.M)

def replace_body(block: str, new_body: str) -> str:
    # Body section = from "### Body\n" to the next article-local heading.
    # Keep media transcripts folded by Step 2d, but discard the publisher's
    # logged-out access CTA that follows the stale body placeholder. The old
    # next-"###" boundary preserved that CTA and made a full-text mega look
    # paywalled even after corpus raw.md replacement.
    m = re.search(r"(^### Body\s*\n)(.*?)(?=\Z)", block, re.S | re.M)
    if not m:
        return block
    tail = m.group(2)
    media = re.findall(r"(?ms)^#{2,3} Media Transcript[^\n]*\n.*?(?=^#{2,3} Media Transcript|\Z)", tail)
    suffix = "\n\n" + "\n\n".join(x.strip() for x in media if x.strip()) if media else ""
    return block[:m.start()] + m.group(1) + "\n" + MARK + "\n\n" + new_body.strip() + suffix + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue_dir")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    # New bundler writes the durable mega directly into the corpus as mega.md;
    # retain support for the historical staging name for backfills.
    megas = glob.glob(os.path.join(a.issue_dir, "mega.md"))
    megas += glob.glob(os.path.join(a.issue_dir, "nejm-*.md"))
    megas = [m for m in megas if not m.endswith(".bak") and ".prefulltext" not in m]
    if len(megas) != 1:
        log(f"expected exactly 1 mega-md in {a.issue_dir}, found {len(megas)}"); sys.exit(1)
    mega = megas[0]
    text = open(mega, encoding="utf-8").read()
    parts = ART_SPLIT.split(text)
    head, blocks = parts[0], parts[1:]
    out, stats = [head], {"corpus": 0, "mineru": 0, "none": 0, "already": 0}
    for blk in blocks:
        dm = DOI_RE.search(blk)
        tm = re.match(r"## Article \d+ — (.+)", blk)
        title = (tm.group(1)[:44] if tm else "?")
        if not dm:
            out.append(blk); continue
        doi = dm.group(1).strip()
        if MARK in blk:
            stats["already"] += 1; log(f"  [already] {title}"); out.append(blk); continue
        ft, src = fulltext(doi, a.issue_dir)
        if ft:
            k = "corpus" if src.startswith("corpus") else "mineru"
            stats[k] += 1
            log(f"  [{k}] {title}  ({len(ft)} chars)")
            out.append(replace_body(blk, ft))
        else:
            stats["none"] += 1
            log(f"  [NONE] {title}  doi={doi} — Body left as-is")
            out.append(blk)
    newtext = "".join(out)
    log(f"\nsummary: corpus={stats['corpus']} mineru={stats['mineru']} none={stats['none']} already={stats['already']}")
    if a.write:
        bak = mega + ".prefulltext.bak"
        if not os.path.exists(bak):
            open(bak, "w", encoding="utf-8").write(text)
        open(mega, "w", encoding="utf-8").write(newtext)
        log(f"WROTE {mega} ({len(newtext)} bytes; backup {os.path.basename(bak)})")
    else:
        log("DRY-RUN (no write). Re-run with --write to apply.")

if __name__ == "__main__":
    main()
