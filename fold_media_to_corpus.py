#!/usr/bin/env python3
"""Persist per-issue journal media (video/audio/supplementary) from the inbox into corpus
per-article bundles, fold transcripts into raw.md (RAG-discoverable), record media in
manifest.json, emit PG source_corpus.source_media rows, and for audio/video-first articles
that have no bundle (no PDF) create a minimal bundle + emit a raw_source_metadata INSERT.

Reusable by backfill (one-off) and the weekly cron (post-promote step). Run on the local
host. Idempotent.

  python3 fold_media_to_corpus.py --journal nejm --date 2026-06-25 [--apply]
"""
import argparse, json, os, re, shutil, sys
from pathlib import Path

from journal_paths import corpus_root

JDIR = {"nejm": "NEJM", "jama": "JAMA", "aim": "AIM", "nature": "Nature",
        "science": "Science", "lancet": "Lancet", "bmj": "BMJ", "jasn": "JASN"}
JOURNAL_NAME = {"nejm": "New England Journal of Medicine", "jama": "JAMA",
                "aim": "Annals of Internal Medicine"}
DOI_PREFIX = {"nejm": "10.1056", "jama": "10.1001", "aim": "10.7326"}
SILENT_MIN_WORDS = 15

def sql_str(s):
    return "NULL" if s is None else "'" + str(s).replace("'", "''") + "'"

def load_mega_titles(mega_path):
    """ckey(lower doi suffix) -> article title, parsed from the issue mega-md."""
    out = {}
    if not os.path.isfile(mega_path):
        return out
    blocks = re.split(r"\n##\s+", open(mega_path, encoding="utf-8").read())
    for b in blocks:
        mdoi = re.search(r"10\.\d{4,9}/([A-Za-z0-9.\-]+)", b)
        if not mdoi:
            continue
        ckey = re.sub(r"[^a-z0-9]", "", mdoi.group(1).lower())
        title = b.splitlines()[0].strip().lstrip("# ").strip() if b.strip() else ""
        title = re.sub(r"^Article\s+\d+\s*[—–-]\s*", "", title)  # drop mega numbering prefix
        if ckey and title and ckey not in out:
            out[ckey] = title
    return out

def fold_transcript(raw_path, kind, media_id, words, text):
    if not os.path.isfile(raw_path):
        return "no-raw"
    body = open(raw_path, encoding="utf-8").read()
    marker = f"## Media Transcript ({kind}): {media_id}"
    if marker in body:
        return "already"
    if words < SILENT_MIN_WORDS:
        section = f"\n\n{marker}\n\n*Silent / figure-only {kind} — no spoken transcript.*\n"
    else:
        section = (f"\n\n{marker}\n\n*Auto-transcribed (whisper-cpp), {words} words; "
                   f"part of the article's full record.*\n\n{text.strip()}\n")
    with open(raw_path, "a", encoding="utf-8") as f:
        f.write(section)
    return "folded"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--apply-pg", dest="apply_pg", action="store_true", help="psql the emitted SQL against the configured PG host")
    a = ap.parse_args()

    jdir = JDIR.get(a.journal, a.journal.upper())
    jname = JOURNAL_NAME.get(a.journal, jdir)
    dprefix = DOI_PREFIX.get(a.journal, "10.0000")
    year = a.date[:4]
    inbox = str(Path(os.environ.get("JOURNAL_INBOX",
                                    str(Path.home() / "Downloads")))
                / "journal-toc" / a.journal / a.date)
    issue_dir = str(corpus_root() / jdir / year / a.date)
    if not os.path.isdir(inbox) or not os.path.isdir(issue_dir):
        print("missing inbox or issue dir"); sys.exit(2)

    mega_titles = load_mega_titles(os.path.join(inbox, f"{a.journal}-{a.date}.md"))
    prefix = f"{a.journal}-{a.date}_"
    media_re = re.compile(re.escape(prefix) + r"(?P<ckey>[a-z0-9]+)_(?P<kind>video|audio|supplementary)_(?P<rest>.+)\.(?P<ext>mp4|mp3|pdf)$")
    issue_audio_re = re.compile(re.escape(prefix) + r"audio_summary_(?P<id>[a-z0-9]+)\.mp3$")

    sql_media, sql_rsm = [], []
    summary = {"video": 0, "audio": 0, "supplementary": 0, "issue_audio": 0,
               "folded": 0, "silent": 0, "created_bundle": 0}
    reindex = set()

    for fn in sorted(os.listdir(inbox)):
        src = os.path.join(inbox, fn)
        if not os.path.isfile(src):
            continue

        mi = issue_audio_re.match(fn)
        if mi:
            dd = os.path.join(issue_dir, "_issue_media")
            tsrc = src.rsplit(".", 1)[0] + ".transcript.txt"
            words = len(open(tsrc, encoding="utf-8").read().split()) if os.path.isfile(tsrc) else 0
            summary["issue_audio"] += 1
            print(f"ISSUE-AUDIO -> _issue_media/ ({words}w)")
            if a.apply:
                os.makedirs(dd, exist_ok=True)
                shutil.copy2(src, os.path.join(dd, f"audio_summary_{mi.group('id')}.mp3"))
                if os.path.isfile(tsrc):
                    shutil.copy2(tsrc, os.path.join(dd, f"audio_summary_{mi.group('id')}.transcript.txt"))
            sql_media.append((f"{a.journal}_issue_{a.date}", "issue_audio",
                              f"_issue_media/audio_summary_{mi.group('id')}.mp3", mi.group('id'),
                              words, words >= SILENT_MIN_WORDS, f"journal/{jdir}/{year}/{a.date}/_issue_media"))
            continue

        m = media_re.match(fn)
        if not m:
            continue
        ckey, kind, rest, ext = m.group("ckey"), m.group("kind"), m.group("rest"), m.group("ext")
        bundle = os.path.join(issue_dir, ckey)
        ck_cf = "NEJM" + ckey[4:] if ckey.startswith("nejm") else ckey  # citation-key casing
        suid = f"doi_{dprefix}_{ck_cf}"
        dst_name = f"{kind}_{rest}.{ext}"
        mid = re.search(r"(NEJMdo\d+|[A-Za-z]+\d{4,})", rest)
        media_id = mid.group(1) if mid else None

        # transcript
        words, text = 0, ""
        if kind in ("video", "audio"):
            tsrc = src.rsplit(".", 1)[0] + ".transcript.txt"
            if os.path.isfile(tsrc):
                text = open(tsrc, encoding="utf-8").read()
                words = len(text.split())

        created = False
        if not os.path.isdir(bundle):
            if kind == "supplementary":
                print(f"SKIP supp {ckey} (no parent bundle)"); continue
            # audio/video-first article with no PDF bundle -> create one
            title = mega_titles.get(ckey, f"{jdir} {ckey}")
            print(f"CREATE-BUNDLE {ckey}  '{title[:48]}'")
            if a.apply:
                os.makedirs(bundle, exist_ok=True)
                raw = (f"---\ntype: raw\nsource_type: journal-article\n"
                       f"doi: {dprefix}/{ck_cf}\ntitle: \"{title}\"\njournal: \"{jname}\"\n"
                       f"year: {year}\nissue_date: {a.date}\nsource_format: {kind}\n---\n\n"
                       f"# {title}\n\n*{jname} — {a.date}. {kind.capitalize()}-first article; "
                       f"the spoken {kind} transcript below is the primary captured record "
                       f"(no article PDF in this issue feed).*\n")
                open(os.path.join(bundle, "raw.md"), "w", encoding="utf-8").write(raw)
                man = {"type": "journal_article", "source_type": "journal-article",
                       "source_uid": suid, "citation_key": ck_cf, "doi": f"{dprefix}/{ck_cf}",
                       "journal": jname, "year": int(year), "issue_date": a.date, "title": title,
                       "source_format": kind}
                json.dump(man, open(os.path.join(bundle, "manifest.json"), "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
                ep = str(corpus_root() / jdir / year / a.date / ckey)
                sql_rsm.append((suid, ck_cf, title, f"{dprefix}/{ck_cf}".lower(), year, jname,
                                ep, f"{ep}/raw.md"))
            created = True
            summary["created_bundle"] += 1

        manifest_p = os.path.join(bundle, "manifest.json")
        man = json.load(open(manifest_p, encoding="utf-8")) if os.path.isfile(manifest_p) else {}

        fold_state = "n/a"
        if kind in ("video", "audio") and a.apply:
            fold_state = fold_transcript(os.path.join(bundle, "raw.md"), kind, media_id or rest, words, text)
            if fold_state == "folded":
                reindex.add(ckey)
                summary["folded" if words >= SILENT_MIN_WORDS else "silent"] += 1
            tsrc = src.rsplit(".", 1)[0] + ".transcript.txt"
            if os.path.isfile(tsrc):
                shutil.copy2(tsrc, os.path.join(bundle, dst_name.rsplit(".", 1)[0] + ".transcript.txt"))

        summary[kind] += 1
        print(f"{kind.upper():13} {ckey}/{dst_name} id={media_id} words={words} fold={fold_state} created={created}")
        dst = os.path.join(bundle, dst_name)
        if a.apply and not (os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src)):
            shutil.copy2(src, dst)

        if a.apply:
            mlist = [x for x in man.get("media", []) if x.get("file") != dst_name]
            e = {"kind": kind, "file": dst_name}
            if media_id:
                e["media_id"] = media_id
            if kind in ("video", "audio"):
                e["transcript_words"] = words
                e["transcript_in_raw_md"] = words >= SILENT_MIN_WORDS
            mlist.append(e)
            man["media"] = mlist
            json.dump(man, open(manifest_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

        sql_media.append((suid, kind, dst_name, media_id,
                          words if kind in ("video", "audio") else None,
                          (words >= SILENT_MIN_WORDS) if kind in ("video", "audio") else False,
                          f"journal/{jdir}/{year}/{a.date}/{ckey}"))

    # SQL out
    mp = os.path.join(inbox, "source_media.sql")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("INSERT INTO source_corpus.source_media (source_uid,kind,file_name,media_id,transcript_words,has_transcript,bundle_path) VALUES\n")
        f.write(",\n".join("(" + ",".join([sql_str(r[0]), sql_str(r[1]), sql_str(r[2]), sql_str(r[3]),
                "NULL" if r[4] is None else str(r[4]), "true" if r[5] else "false", sql_str(r[6])]) + ")" for r in sql_media))
        f.write("\nON CONFLICT (source_uid,file_name) DO UPDATE SET transcript_words=EXCLUDED.transcript_words, has_transcript=EXCLUDED.has_transcript, bundle_path=EXCLUDED.bundle_path;\n")
    rp = os.path.join(inbox, "source_rsm.sql")
    if sql_rsm:
        with open(rp, "w", encoding="utf-8") as f:
            for r in sql_rsm:
                f.write("INSERT INTO wiki_raw.raw_source_metadata (source_uid,citation_key,source_type,sidecar_key,title,doi,year,journal,end_folder_path,raw_md_path,mineru_status,ingest_status,payload_flags) VALUES ("
                        + ",".join([sql_str(r[0]), sql_str(r[1]), "'journal-article'", sql_str(r[1]), sql_str(r[2]),
                                    sql_str(r[3]), str(r[4]), sql_str(r[5]), sql_str(r[6]), sql_str(r[7]),
                                    "'not_started'", "'promoted'", "'{audio_only}'::text[]"]) + ") ON CONFLICT DO NOTHING;\n")

    if a.apply_pg and a.apply:
        import subprocess
        psql_bin = os.environ.get("PSQL_BIN", "psql")
        for f in ([rp] if sql_rsm else []) + [mp]:
            r = subprocess.run([psql_bin, "-h", os.environ.get("PGHOST", "localhost"),
                                "-U", os.environ.get("PGUSER", "postgres"),
                                "-d", os.environ.get("PGDATABASE", "journal_bundler"),
                                "-f", f], capture_output=True, text=True)
            print("PG-APPLY", os.path.basename(f), "rc", r.returncode, (r.stdout + r.stderr).strip()[-160:])

    print("\n=== SUMMARY ===", json.dumps(summary))
    print("reindex:", sorted(reindex))
    print("media_sql:", mp, len(sql_media), "rows;  rsm_sql:", rp if sql_rsm else "(none)", len(sql_rsm))

if __name__ == "__main__":
    main()
