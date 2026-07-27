#!/usr/bin/env python3
"""NEJM online-first watcher — notify-only.

Polls NEJM's "Recently Published" listing
(https://www.nejm.org/toc/nejm/recently-published) via the existing logged-in
browser session (the same Cloudflare-clearing in-page credentialed XHR the
weekly bundler uses), diffs article DOIs against
admin_ops.nejm_online_first_seen, and NOTIFIES once per newly-seen article.

WHY: NEJM publishes articles online-first, days ahead of the Thursday issue. The
weekly bundler job only reads /toc/nejm/current, so an online-first article
(e.g. NEJMoa2605555, published 2026-05-31) is invisible to the weekly pipeline
until it is assigned to an issue. This watcher closes that latency gap.

NOTIFY-ONLY: does NOT ingest. Ingest stays on-demand via
`bundler.py nejm --dois <doi> --force-ingest-all`.

Reuses bundler.parse_toc() with a cloned NEJM jcfg whose toc_url points at the
recently-published listing — parseNEJM_TOC handles that DOM unchanged (verified
2026-06-01: 49 articles parsed, incl. NEJMoa2605555).

State : admin_ops.nejm_online_first_seen (doi PK, title, url, pub_type,
        first_seen, notified_at).
PG    : psql shell-out, same idiom as bundler.lookup_existing_payload_path
        (host = $PGHOST, -U $PGUSER -d $PGDATABASE).
Notify: --notify {stdout|telegram|gmail}; default stdout (dry run, no send).

Usage:
  nejm_online_first_watch.py --seed             # mark all current as seen, no notify
  nejm_online_first_watch.py --notify stdout    # dry run: print new articles
  nejm_online_first_watch.py --notify telegram  # push new articles to the configured recipient
  nejm_online_first_watch.py --notify gmail     # email new articles to the configured recipient
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))
import bundler  # noqa: E402  same-dir import; provides parse_toc + JOURNALS

PSQL = os.environ.get("PSQL_BIN", "psql")
if not Path(PSQL).exists():
    PSQL = "psql"
PG_HOST = os.environ.get("PGHOST", "localhost")
PG_USER = os.environ.get("PGUSER", "postgres")
PG_DB = os.environ.get("PGDATABASE", "journal_bundler")

RECENTLY_PUBLISHED_URL = "https://www.nejm.org/toc/nejm/recently-published"
TG_NOTIFY = os.environ.get("JOURNAL_TG_NOTIFY_SCRIPT", "")
GMAIL_ENV = os.environ.get("JOURNAL_GMAIL_ENV", "")
LOCK_DIR = Path("/tmp/nejm-online-first-watch.lock.d")

# NEJM article-id prefix -> human label (best-effort; unknown codes pass through)
_NEJM_TYPE = {
    "oa": "Original Article", "e": "Editorial", "p": "Perspective",
    "ra": "Review Article", "cp": "Clinical Practice", "c": "Correspondence",
    "icm": "Images in Clin Med", "cpc": "Case Records MGH",
    "cps": "Clinical Problem-Solving", "sr": "Special Report",
    "sb": "Sounding Board", "do": "Interactive/Media", "ms": "Medicine & Society",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}", flush=True)


def nejm_type(doi: str | None) -> str | None:
    m = re.search(r"NEJM([a-z]+)\d", doi or "", re.I)
    if not m:
        return None
    code = m.group(1).lower()
    return _NEJM_TYPE.get(code, code)


# ---- PG helpers (psql shell-out, same idiom as bundler) --------------------
def _psql(sql: str, *, rows: bool = False, timeout: int = 20):
    r = subprocess.run(
        [PSQL, "-h", PG_HOST, "-U", PG_USER, "-d", PG_DB,
         "-tA", "-F", "\t", "-c", sql],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed (rc={r.returncode}): {r.stderr.strip()}")
    if rows:
        return [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]
    return r.stdout.strip()


def _q(s) -> str:
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def ensure_table() -> None:
    _psql(
        "CREATE TABLE IF NOT EXISTS admin_ops.nejm_online_first_seen ("
        " doi text PRIMARY KEY,"
        " title text,"
        " url text,"
        " pub_type text,"
        " first_seen timestamptz NOT NULL DEFAULT now(),"
        " notified_at timestamptz);"
    )


def seen_dois() -> set:
    out = _psql("SELECT doi FROM admin_ops.nejm_online_first_seen;", rows=True)
    return {r[0] for r in out if r and r[0]}


def record(items: list, *, notified: bool) -> None:
    if not items:
        return
    notified_val = "now()" if notified else "NULL"
    values = ",".join(
        f"({_q(a['doi'])},{_q(a.get('title'))},{_q(a.get('article_url'))},"
        f"{_q(a.get('pub_type'))},{notified_val})"
        for a in items
    )
    _psql(
        "INSERT INTO admin_ops.nejm_online_first_seen "
        "(doi,title,url,pub_type,notified_at) VALUES "
        f"{values} ON CONFLICT (doi) DO NOTHING;"
    )


# ---- fetch + format --------------------------------------------------------
def fetch_recently_published(limit: int | None = None) -> list:
    jcfg = copy.deepcopy(bundler.JOURNALS["nejm"])
    jcfg["toc_url"] = RECENTLY_PUBLISHED_URL
    arts = [a for a in bundler.parse_toc(jcfg) if a.get("doi")]
    return arts[:limit] if limit else arts


def format_items(items: list) -> str:
    lines = []
    for a in items:
        t = a.get("pub_type") or "?"
        lines.append(f"• [{t}] {a.get('title', '(no title)')}\n  {a.get('article_url')}")
    return "\n".join(lines)


# ---- notifiers -------------------------------------------------------------
def notify_stdout(items: list) -> None:
    print(f"=== {len(items)} new NEJM online-first article(s) ===")
    print(format_items(items))


def notify_telegram(items: list) -> None:
    if not TG_NOTIFY:
        raise RuntimeError(
            "telegram notify requires JOURNAL_TG_NOTIFY_SCRIPT to point at a "
            "notify script (argv: --title <t> --lane <l> --stdin)")
    r = subprocess.run(
        ["python3", str(TG_NOTIFY),
         "--title", f"\U0001fa7a NEJM online-first — {len(items)} new",
         "--lane", "nejm-online-first",
         "--stdin"],
        input=format_items(items), capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"telegram-notify failed: {r.stderr.strip()} {r.stdout.strip()}")


def _load_env(path: Path) -> dict:
    d: dict = {}
    if not path.exists():
        return d
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def notify_gmail(items: list) -> None:
    import smtplib
    from email.message import EmailMessage

    if not GMAIL_ENV:
        raise RuntimeError(
            "gmail notify requires JOURNAL_GMAIL_ENV to point at an env file "
            "exporting GMAIL_ADDR + GMAIL_APP_PASSWORD (+ optional GMAIL_TO)")
    env = _load_env(Path(GMAIL_ENV))
    addr = env.get("GMAIL_ADDR") or os.environ.get("GMAIL_ADDR")
    pw = env.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD")
    to = env.get("GMAIL_TO") or addr
    if not addr or not pw:
        raise RuntimeError(
            f"gmail send needs GMAIL_ADDR + GMAIL_APP_PASSWORD in {GMAIL_ENV}")
    msg = EmailMessage()
    msg["Subject"] = f"NEJM online-first — {len(items)} new"
    msg["From"] = addr
    msg["To"] = to
    msg.set_content(format_items(items))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(addr, pw)
        s.send_message(msg)


NOTIFIERS = {"stdout": notify_stdout, "telegram": notify_telegram, "gmail": notify_gmail}


# ---- main ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="NEJM online-first watcher (notify-only).")
    ap.add_argument("--notify", choices=list(NOTIFIERS), default="stdout")
    ap.add_argument("--seed", action="store_true",
                    help="mark all current articles as seen WITHOUT notifying")
    ap.add_argument("--limit", type=int, default=None, help="cap TOC items parsed (debug)")
    ap.add_argument("--max-notify", type=int, default=15,
                    help="cap notifications per run (backlog blast guard)")
    args = ap.parse_args()

    try:
        LOCK_DIR.mkdir()
    except FileExistsError:
        log("another run holds the lock; exiting")
        return 0
    try:
        ensure_table()
        arts = fetch_recently_published(limit=args.limit)
        log(f"fetched {len(arts)} articles from recently-published")
        for a in arts:
            a["pub_type"] = nejm_type(a.get("doi"))
        seen = seen_dois()
        new = [a for a in arts if a["doi"] not in seen]
        log(f"{len(new)} new (not in seen table)")

        if args.seed:
            record(arts, notified=True)
            log(f"seeded {len(arts)} articles as seen (no notify)")
            return 0
        if not new:
            log("nothing new; done")
            return 0

        to_notify = new[:args.max_notify]
        if len(new) > args.max_notify:
            log(f"WARN {len(new)} new exceeds max-notify={args.max_notify}; "
                f"notifying first {args.max_notify}, recording all as seen")
        NOTIFIERS[args.notify](to_notify)
        record(new, notified=True)
        log(f"notified {len(to_notify)} via {args.notify}; recorded {len(new)} as seen")
        return 0
    finally:
        try:
            LOCK_DIR.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
