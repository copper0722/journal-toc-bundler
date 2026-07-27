#!/usr/bin/env python3
"""journal-issue bundler — drives a logged-in browser tab via AppleScript to
extract the current week's full issue from a publisher TOC into:

  * one mega .md  ("raw of raw") at
    wiki_raw/_journal_toc/{journal}/{YYYY-MM-DD}/{YYYY-MM-DD}.md
  * (optional, when journal config has wiki_ingest_all=true) per-article
    payload folders at
    wiki_raw/_journal_toc/{journal}/{YYYY-MM-DD}/articles/{citation_key}/
    holding raw.md + manifest.json (and source.pdf if fetch_pdf=true).

  Layout switched 2026-05-14 from `articles/{journal}_weekly_issue/...` to
  `journal_toc/{journal}/{date}/...`: TOC ≠ single articles; staging belongs
  in a journal_toc namespace, not /articles.

Per-journal config: journals.json (alongside this script).
JS extraction layer: extractor.js (sync XHR + DOM parsers).
Browser bridge:        run_in_browser.applescript.

Usage:
  bundler.py aim                       # AIM current issue, today as date
  bundler.py nejm                      # NEJM current issue
  bundler.py aim --issue-date 2026-05-05
  bundler.py nejm --limit 3            # only first 3 articles (smoke test)
  bundler.py nejm --no-pdf             # mega md only, skip PDFs
  bundler.py nejm --skip-toc           # use cached TOC (rerun extraction only)

Designed to run command-triggered (no scheduling). Per-article extraction
takes ~5-10 sec per article; 30-50-article issues finish in a few minutes.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
# Runtime staging is an inbox artifact, not website source. WIKI_RAW remains an
# environment override for compatibility with older invocations.
WIKI_RAW = Path(os.environ.get(
    "JOURNAL_TOC_STAGING",
    os.environ.get("WIKI_RAW", str(Path.home() / "Downloads" / "journal-toc" / "_staging")),
))
JOURNALS = json.loads((ROOT / "journals.json").read_text(encoding="utf-8"))
EXTRACTOR_JS = ROOT / "extractor.js"
APPLESCRIPT = ROOT / "run_in_browser.applescript"
TST = timezone(timedelta(hours=8))

# Optional podcast-harvest companion (only used when journals.json
# fetch_podcast_transcripts=true for the current journal). Lazy import so the
# bundler stays runnable when the companion file is missing.
sys.path.insert(0, str(ROOT))
try:
    import podcast_bundler as _podcast
except ImportError:
    _podcast = None
from journal_paths import mega_path, corpus_root

# Optional Crossref abstract enricher (step 2a). Deterministic, server-side
# by-DOI abstract fetch so the reading-guide harvest no longer depends on a
# warm Cloudflare-cleared browser tab. Lazy import keeps the bundler runnable
# if the companion file is missing.
try:
    import crossref_enrich as _crossref
except ImportError:
    _crossref = None

# Polite-pool contact for the Crossref API (step 2a). Override via env if needed.
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "journal-bundler@example.com")

# PostgreSQL connection — every value is env-overridable with a public-safe
# default. The tool expects the `wiki_raw` schema documented in the README
# (create it with the provided DDL if present).
PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGUSER = os.environ.get("PGUSER", "postgres")
PGDATABASE = os.environ.get("PGDATABASE", "journal_bundler")
PSQL_BIN = os.environ.get("PSQL_BIN", "psql")


def now_iso() -> str:
    return datetime.now(TST).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(TST).date().isoformat()


def _modal_issue_date(articles: list) -> str | None:
    """Real issue date = the modal article publication date (citation_publication_date),
    normalized to YYYY-MM-DD. The bundler's RUN date is NOT the issue date (key
    TOC bundles by the actual issue — e.g. JAMA Vol 335 No 23
    = 2026-06-16 — never the day the bundler ran). Recurring sections (audio / this-week)
    carry older dates; the issue's own articles dominate, so the mode (tie-break: latest)
    is the issue date. Returns None when too few/too-scattered dated articles to be
    confident, in which case the caller keeps the run-date."""
    from collections import Counter
    dates = []
    for a in articles:
        pd = str(a.get("pub_date") or "").strip()
        m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", pd)
        if m:
            dates.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    if len(dates) < 3:
        return None
    counts = Counter(dates)
    top_n = max(counts.values())
    if top_n < max(2, len(dates) * 0.35):   # mode must cover >=35% of dated articles
        return None
    return sorted([d for d, n in counts.items() if n == top_n], reverse=True)[0]


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _toc_stated_issue_date(articles: list, run_date: str) -> str | None:
    """Extract the issue's STATED publication date from the TOC/article content
    (e.g. Science '09 Jul 2026', Lancet 'July 11, 2026'). Used when articles carry
    no structured pub_date (Science/Lancet RSS) so _modal_issue_date is blind and
    the caller would otherwise keep the RUN date — the 2026-07-10 bug where Science
    (real 07-09) and Lancet (real 07-11) both got dated 07-10 (Friday capture date),
    producing duplicate wrong-date issue folders. Scans titles/sections/bodies for
    'DD Mon YYYY' + 'Month DD, YYYY', keeps only dates within +/-14 days of the run
    date (the issue date is near the run; filters out cited/reference dates), returns
    the mode (tie-break: nearest to run date). None if no confident in-window match."""
    from collections import Counter
    from datetime import datetime as _dt
    try:
        run = _dt.strptime(run_date, "%Y-%m-%d").date()
    except Exception:
        return None
    parts = []
    for a in articles:
        for k in ("title", "section", "article_type", "body_md"):
            v = a.get(k)
            if v:
                parts.append(str(v)[:3000])
    text = "\n".join(parts)
    cand = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})\b", text):
        mo = _MONTHS.get(m.group(2)[:3].lower())
        if mo:
            try:
                cand.append(_dt(int(m.group(3)), mo, int(m.group(1))).date())
            except ValueError:
                pass
    for m in re.finditer(r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s+(20\d{2})\b", text):
        mo = _MONTHS.get(m.group(1)[:3].lower())
        if mo:
            try:
                cand.append(_dt(int(m.group(3)), mo, int(m.group(2))).date())
            except ValueError:
                pass
    inwin = [d for d in cand if abs((d - run).days) <= 14]
    if not inwin:
        return None
    counts = Counter(inwin)
    top = max(counts.values())
    best = sorted([d for d, n in counts.items() if n == top],
                  key=lambda d: abs((d - run).days))[0]
    return best.isoformat()


def osa_run(browser: str, tab_match: str, op: str, op_args: dict, *, retries: int = 2,
            timeout: int = 180, backoff: int = 30) -> str:
    """Dispatch into the JS layer. Returns the raw JSON string produced.

    Retries on `subprocess.TimeoutExpired` (Chrome Beta AppleEvent hang) — the
    failure pattern seen on 2026-05-28 06:00 + 07:00 NEJM cron, where both
    scheduled attempts crashed with TimeoutExpired against parseToc and the
    issue went unbundled until a manual mid-day rerun. `retries=2` means up
    to 3 total attempts (1 + 2 retries) with linear backoff between tries.
    Other RuntimeErrors (osascript stderr, ERR: prefix) are not retried —
    they indicate a structural failure (extractor JS exception, bad tab
    match) that won't self-heal.
    """
    op_json = json.dumps(op_args)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                ["osascript", str(APPLESCRIPT), browser, tab_match, str(EXTRACTOR_JS), op, op_json],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = r.stdout.strip()
            if not out or out.startswith("ERR:"):
                raise RuntimeError(f"osascript failed: {out!r} stderr={r.stderr.strip()!r}")
            return out
        except subprocess.TimeoutExpired as e:
            last_exc = e
            if attempt < retries:
                wait = backoff * (attempt + 1)
                print(f"  WARN osa_run timeout on attempt {attempt + 1}/{retries + 1} for op={op} "
                      f"(tab={tab_match}); retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    assert last_exc is not None
    raise last_exc


_CF_CHALLENGE_TERMS = ("請稍候", "Just a moment", "Verifying",
                       "Attention Required", "Checking your browser")


def ensure_cleared_tab(browser: str, url: str, *, max_wait: int = 60,
                       poll: int = 3) -> bool:
    """Guarantee a Cloudflare-CLEARED tab for `url` exists in `browser`.

    Closes domain tabs stuck on a CF
    challenge + duplicate domain tabs, force-opens `url`, then polls the tab
    title until it is no longer a challenge page (or max_wait elapses). Returns
    True if a cleared tab is confirmed.

    Why this exists: NEJM full text + PDF are real-browser-fingerprinted by the
    publisher (curl_cffi gets a stub body / HTML-instead-of-PDF after the
    cookie session degrades ~1h), so the BROWSER is the reliable path for the
    per-article extract/PDF phase — and it only fails when its tab is stuck on
    a Cloudflare challenge, which is exactly what 403'd the 2026-06-18 cron
    (both 06:00 + 07:00). Active clearance here + the run_in_browser.applescript
    skip-challenge-tab selection together make that phase robust. ensure_tab_open
    (below) is passive/idempotent and does NOT self-heal a stuck tab; prefer
    this for journals behind Cloudflare.
    """
    domain = urldomain(url)
    isch = " or ".join(f'(ti contains "{t}")' for t in _CF_CHALLENGE_TERMS)
    # 1. close challenge + duplicate domain tabs (reverse index; no JS exec → safe)
    close_as = (
        f'tell application "{browser}"\n'
        f'  set seen to 0\n'
        f'  repeat with w in windows\n'
        f'    set tl to tabs of w\n'
        f'    repeat with i from (count of tl) to 1 by -1\n'
        f'      set tt to item i of tl\n'
        f'      set u to ""\n      try\n        set u to (URL of tt as string)\n      end try\n'
        f'      if u contains "{domain}" then\n'
        f'        set ti to ""\n        try\n          set ti to (title of tt as string)\n        end try\n'
        f'        if ({isch}) or (seen > 0) then\n          close tt\n'
        f'        else\n          set seen to seen + 1\n        end if\n'
        f'      end if\n'
        f'    end repeat\n'
        f'  end repeat\n'
        f'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", close_as],
                       capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pass
    # 2. force-open the exact url to land fresh CF clearance
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "{browser}"\n  activate\n  open location "{url}"\nend tell'],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pass
    # 3. poll the domain tab's title until it is no longer a CF challenge
    probe_as = (
        f'tell application "{browser}"\n'
        f'  repeat with w in windows\n'
        f'    repeat with t in tabs of w\n'
        f'      set u to ""\n      try\n        set u to (URL of t as string)\n      end try\n'
        f'      if u contains "{domain}" then\n'
        f'        try\n          return (title of t as string)\n        end try\n'
        f'      end if\n'
        f'    end repeat\n'
        f'  end repeat\n'
        f'  return "__none__"\n'
        f'end tell'
    )
    waited = 0
    while waited < max_wait:
        try:
            r = subprocess.run(["osascript", "-e", probe_as],
                               capture_output=True, text=True, timeout=20)
            title = (r.stdout or "").strip()
        except subprocess.TimeoutExpired:
            title = ""
        if title and title != "__none__" and not any(
                c in title for c in _CF_CHALLENGE_TERMS):
            return True
        time.sleep(poll)
        waited += poll
    return False


def ensure_tab_open(browser: str, url: str, wait_seconds: int = 8) -> None:
    """Open URL in `browser` if no tab already matches the domain. Wait for load.

    Idempotent: if a matching tab exists, no-op. Used to bootstrap subscriber
    sessions for cron-triggered runs that can't assume a tab is pre-opened.
    """
    domain = urldomain(url)
    # First check if matching tab already exists via a lightweight osascript probe.
    # On probe timeout (Chrome Beta AppleEvent -1712 hangs under load) degrade to
    # n=0 so we still attempt open location below — Chrome navigates idempotently
    # for an already-open URL, so worst case is no-op rather than a hard fail.
    try:
        probe = subprocess.run(
            ["osascript", "-e",
             f'tell application "{browser}" to set tabs_found to 0\n'
             f'tell application "{browser}"\n'
             f'  repeat with w in windows\n'
             f'    repeat with t in tabs of w\n'
             f'      if (URL of t as string) contains "{domain}" then set tabs_found to tabs_found + 1\n'
             f'    end repeat\n'
             f'  end repeat\n'
             f'end tell\n'
             f'return tabs_found'],
            capture_output=True, text=True, timeout=30,
        )
        n = int(probe.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, AttributeError):
        n = 0
    if n > 0:
        return
    # No matching tab (or probe wedged) — open one. Tolerate timeout same way:
    # the AppleEvent may still take effect even when the Python side times out.
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "{browser}"\n'
             f'  activate\n'
             f'  open location "{url}"\n'
             f'end tell'],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        pass
    time.sleep(wait_seconds)


# ---------------------------------------------------------------------------
# Server-side RSS TOC fetch (Science AAAS RSS, Lancet Elsevier RDF/RSS).
# Both journals' TOC source is a namespace-rich RSS/RDF feed that passes plain
# server-side fetch (no Cloudflare challenge) — routing it through the Chrome
# Beta osascript bridge is unnecessary and was the cause of the 2026-05-29
# 180s parseToc hang on BOTH journals (no TOC produced). parse_toc() tries this
# path first for toc_format=="rss" and falls back to the browser path on any
# error, so there is zero regression for browser-only journals. These are
# faithful Python ports of extractor.js parseScience_TOC / parseLancet_TOC.
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 30) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _rss_items(xml_text: str) -> list:
    """All <item> elements across RSS 2.0 and RSS 1.0/RDF, namespace-agnostic."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    return [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "item"]


def _item_text(item, localname: str) -> str:
    """First direct child of `item` whose local tag-name == localname."""
    for ch in item:
        if ch.tag.rsplit("}", 1)[-1] == localname:
            return (ch.text or "").strip()
    return ""


def _science_type_code(section: str) -> str:
    if not section:
        return ""
    n = section.lower()
    if "research article" in n: return "research"
    if "review" in n: return "review"
    if n.startswith("books"): return "books"
    if "editorial" in n: return "editorial"
    if "news" in n: return "news"
    if "perspective" in n: return "perspective"
    if "letter" in n: return "letter"
    if "policy" in n: return "policy"
    if "working life" in n: return "working_life"
    if "insight" in n: return "insight"
    return re.sub(r"^_|_$", "", re.sub(r"[^a-z0-9]+", "_", n))[:30]


def _lancet_type_code(section: str) -> str:
    if not section:
        return ""
    n = section.lower()
    if n == "articles": return "research"
    if n == "editorial": return "editorial"
    if n == "comment": return "comment"
    if n == "correspondence": return "correspondence"
    if n == "perspectives": return "perspective"
    if n == "world report": return "news"
    if n == "obituary": return "obituary"
    if n == "department of error": return "erratum"
    if "commission" in n: return "commission"
    if "seminar" in n: return "review"
    if "review" in n: return "review"
    if "series" in n: return "review"
    if "viewpoint" in n: return "perspective"
    if "news" in n: return "news"
    return re.sub(r"^_|_$", "", re.sub(r"[^a-z0-9]+", "_", n))[:30]


def parse_science_rss(xml_text: str) -> list:
    out = []
    for item in _rss_items(xml_text):
        title = re.sub(r"\s+", " ", _item_text(item, "title")).strip()
        doi = _item_text(item, "doi")  # prism:doi
        if not doi:
            m = re.search(r"(10\.1126/[^?#\s]+)", _item_text(item, "identifier"))
            if m: doi = m.group(1)
        if not doi:
            m = re.search(r"(10\.1126/[^?#\s]+)", _item_text(item, "link"))
            if m: doi = m.group(1)
        if not doi:
            continue
        section = _item_text(item, "type")  # dc:type
        out.append({
            "doi": doi, "title": title, "section": section,
            "type_code": _science_type_code(section), "is_oa": False,
            "article_url": "https://www.science.org/doi/" + doi,
            "pdf_url": "https://www.science.org/doi/pdf/" + doi,
        })
    return out


def parse_lancet_rss(xml_text: str) -> list:
    out = []
    for item in _rss_items(xml_text):
        doi = ""
        m = re.search(r"(10\.1016/S0140-6736\(\d{2}\)\d{5}-[0-9X])",
                      _item_text(item, "identifier"))
        if m: doi = m.group(1)
        if not doi:
            m = re.search(r"PII(S0140-6736\(\d{2}\)\d{5}-[0-9X])",
                          _item_text(item, "link"))
            if m: doi = "10.1016/" + m.group(1)
        if not doi:
            continue
        pii = re.sub(r"^10\.1016/", "", doi)
        raw_title = re.sub(r"\s+", " ", _item_text(item, "title")).strip()
        title = re.sub(r"^\[[^\]]+\]\s*", "", raw_title)  # strip [Section] prefix
        section = _item_text(item, "section")  # prism:section
        out.append({
            "doi": doi, "title": title, "section": section,
            "type_code": _lancet_type_code(section), "is_oa": False,
            "rss_abstract": re.sub(r"\s+", " ", _item_text(item, "description")).strip(),
            "article_url": "https://www.thelancet.com/journals/lancet/article/PII" + pii + "/fulltext",
            "pdf_url": "https://www.thelancet.com/pdfs/journals/lancet/PII" + pii + ".pdf",
        })
    return out


def _jfda_type_code(section: str) -> str:
    n = (section or "").lower()
    if "review" in n: return "review"
    if "original" in n: return "research"
    if "corrigend" in n or "erratum" in n or "errata" in n: return "erratum"
    if "acknowledg" in n: return "acknowledgment"
    if "editorial" in n: return "editorial"
    if "letter" in n or "correspond" in n: return "correspondence"
    return re.sub(r"^_|_$", "", re.sub(r"[^a-z0-9]+", "_", n))[:30]


def parse_jfda_html(html_text: str) -> list:
    """JFDA (Journal of Food and Drug Analysis) — bepress Digital Commons OA
    journal, fully server-side fetchable (no Cloudflare, no subscriber session).
    DOI is deterministic from the bepress article id: 10.38212/2224-6614.<id>,
    so the home/issue TOC alone yields doi+title+section+pdf without per-article
    fetches. Fully server-side fetchable OA journal — needs no cross-wall
    session, unlike paywalled publishers (e.g. Springer) behind a login."""
    import html as _h
    sections = [(m.start(), re.sub(r"<[^>]+>", "", m.group(1)).strip())
                for m in re.finditer(r'<h2 id="[^"]+"[^>]*>(.*?)</h2>', html_text, re.S)]
    def section_for(pos: int) -> str:
        s = ""
        for spos, st in sections:
            if spos < pos:
                s = st
            else:
                break
        return s
    land_re = re.compile(r'href="(https://www\.jfda-online\.com/journal/vol\d+/iss\d+/\d+)"')
    landings = [(m.start(), m.group(1)) for m in land_re.finditer(html_text)]
    pdf_re = re.compile(
        r'<a href="[^"]*viewcontent\.cgi\?article=(\d+)[^"]*"[^>]*?'
        r'title="Download PDF of (.+?) \([0-9.]+&nbsp;[KMG]B\)"', re.S)
    out = []
    for m in pdf_re.finditer(html_text):
        pos, aid, title = m.start(), m.group(1), _h.unescape(m.group(2)).strip()
        sect = section_for(pos)
        tcode = _jfda_type_code(sect)
        if tcode == "acknowledgment":
            continue  # reviewer-acknowledgment list is not a citable article
        land = next((l[1] for l in landings if l[0] > pos), None)
        doi = f"10.38212/2224-6614.{aid}"
        # OA bonus: the bepress landing page carries the full abstract (meta
        # description) + author list, fetchable server-side with no session —
        # which is exactly the talk's OA-vs-paywall contrast point.
        abstract, authors = "", ""
        if land:
            try:
                lh = _http_get(land + "/", timeout=20)
                am = re.search(r'<meta name="description" content="([^"]*)"', lh)
                if am:
                    abstract = _h.unescape(am.group(1)).strip()
                authors = "; ".join(_h.unescape(a) for a in re.findall(
                    r'<meta name="bepress_citation_author" content="([^"]*)"', lh))
            except Exception:
                pass
        out.append({
            "doi": doi, "title": title, "section": sect,
            "type_code": tcode, "is_oa": True,
            "abstract": abstract, "authors": authors,
            "article_url": (land + "/") if land else f"https://doi.org/{doi}",
            "pdf_url": (f"https://www.jfda-online.com/cgi/viewcontent.cgi?"
                        f"article={aid}&context=journal"),
        })
    return out


def _elsevier_type_code(section: str) -> str:
    if not section:
        return ""
    n = section.lower()
    if "research" in n or "original" in n: return "research"
    if "review" in n: return "review"
    if "editorial" in n: return "editorial"
    if "commentary" in n or "comment" in n: return "comment"
    if "correspondence" in n or "letter" in n: return "correspondence"
    if "perspective" in n: return "perspective"
    if "guideline" in n or "kdigo" in n: return "guideline"
    if "nephrology image" in n or "teaching case" in n or "case" in n: return "case"
    if "news" in n: return "news"
    if "erratum" in n or "corrigendum" in n or "correction" in n: return "erratum"
    return re.sub(r"^_|_$", "", re.sub(r"[^a-z0-9]+", "_", n))[:30]


def parse_elsevier_rdf_rss(xml_text: str) -> list:
    """Generic Elsevier RDF/RSS 1.0 parser (Kidney International, AJKD, and any
    ScienceDirect journal whose /current.rss carries <dc:identifier> DOI +
    <prism:section> + <description> abstract). DOI verbatim from <dc:identifier>
    (KI 10.1016/j.kint.*, AJKD 10.1053/j.ajkd.* are NOT PII-derivable). Crossref
    deposits no abstracts for Elsevier, so the RSS <description> IS the abstract
    source (step-2a Crossref is a no-op backstop).

    ISSUE FILTER. /current.rss is NOT a single issue —
    it MIXES the current PRINT issue (items carrying prism:volume + prism:number)
    with online-first / articles-in-press that have NO volume/number yet. Without
    filtering the bundler ingested the whole firehose as 'this issue' (KI fed 102
    when Vol 110 No 1 actually has 39 — the old 'KI 122/125' counts were the same
    bug). Fix: keep ONLY the latest (volume, number) present; drop online-first +
    older issues. Falls back to keep-all only when NO item carries issue metadata."""
    items = _rss_items(xml_text)
    issue_counts: dict = {}
    for it in items:
        v, n = _item_text(it, "volume"), _item_text(it, "number")
        if v and n:
            try:
                key = (int(v), int(n))
            except ValueError:
                continue
            issue_counts[key] = issue_counts.get(key, 0) + 1
    target = max(issue_counts) if issue_counts else None  # latest volume, then issue number
    out, dropped = [], 0
    for item in items:
        ident = _item_text(item, "identifier")
        m = re.search(r"(10\.\d{4,9}/[^\s]+)", ident)
        if not m:
            continue
        if target is not None:
            v, n = _item_text(item, "volume"), _item_text(item, "number")
            try:
                in_issue = bool(v and n and (int(v), int(n)) == target)
            except ValueError:
                in_issue = False
            if not in_issue:
                dropped += 1
                continue
        doi = m.group(1).strip()
        title = re.sub(r"\s+", " ", _item_text(item, "title")).strip()
        section = _item_text(item, "section")
        link = (_item_text(item, "link") or "").split("?")[0]
        out.append({
            "doi": doi, "title": title, "section": section,
            "type_code": _elsevier_type_code(section), "is_oa": False,
            "rss_abstract": re.sub(r"\s+", " ", _item_text(item, "description")).strip(),
            "article_url": link or f"https://doi.org/{doi}",
            "pdf_url": link or f"https://doi.org/{doi}",
        })
    if target is not None:
        print(f"  parse_elsevier_rdf_rss: current issue Vol {target[0]} No {target[1]} "
              f"-> {len(out)} articles (dropped {dropped} online-first/other-issue)")
    return out


_RSS_SERVERSIDE_PARSERS = {
    "science": parse_science_rss,
    "lancet": parse_lancet_rss,
    "elsevier": parse_elsevier_rdf_rss,
}
_HTML_SERVERSIDE_PARSERS = {"jfda": parse_jfda_html}


def parse_nejm_toc_html(html: str) -> list:
    """Faithful Python port of extractor.js `parseNEJM_TOC` (DOM parser).

    Parses NEJM current-issue TOC HTML into the SAME article dicts the browser
    `parseToc` path returns. Consumed by the cookie+curl_cffi `parse_toc` path
    so a scheduled TOC harvest no longer depends on a live, Cloudflare-cleared
    browser tab — a failure mode where the parseToc XHR fires from a tab stuck
    on the CF challenge and 403s. Any
    failure here falls through to the browser path (zero regression).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    type_labels = {
        "oa": "Original Article", "ra": "Review", "p": "Perspective",
        "e": "Editorial", "c": "Correspondence", "cpc": "Case Records",
        "icm": "Images", "x": "Correction", "sa": "Special Article",
    }
    out = []
    for item in soup.select(".issue-item"):
        doi_input = item.select_one("input.inputDoi")
        if not doi_input:
            continue
        doi = (doi_input.get("value") or "").strip()
        if not doi:
            continue
        article_id = doi.replace("10.1056/", "")
        title_a = item.select_one('.issue-item_title a[href*="/doi/full/"]')
        title = (re.sub(r"\s+", " ", title_a.get_text()).strip()
                 if title_a else article_id)
        m = re.match(r"^NEJM([a-z]+)", article_id, re.I)
        type_code = m.group(1).lower() if m else ""
        has_video = item.select_one('[data-original-title="Video"]') is not None
        out.append({
            "doi": doi,
            "title": title,
            "section": type_labels.get(type_code, type_code.upper()),
            "type_code": type_code,
            "is_oa": False,  # NEJM TOC has no reliable OA marker
            "article_url": "https://www.nejm.org/doi/full/" + doi,
            "pdf_url": "https://www.nejm.org/doi/pdf/" + doi,
            "has_video": has_video,
        })
    return out


# Cookie+curl_cffi TOC parsers, keyed by jcfg["cookie_auth"]["parser"]. The
# fetch is done by _journal_fetcher_lib (Chrome-impersonation + dumped session
# cookies, Cloudflare-proof); these parse the returned HTML in pure Python.
_COOKIE_TOC_PARSERS = {"nejm": parse_nejm_toc_html}


def parse_toc(jcfg: dict, issue_date: str | None = None) -> list:
    # Server-side RSS path first for RSS-sourced journals (Science/Lancet); the
    # feed passes plain fetch, avoiding the Chrome-Beta osascript hang. Any
    # failure falls through to the browser path below (zero regression).
    if jcfg.get("toc_format") == "rss":
        parser = _RSS_SERVERSIDE_PARSERS.get(jcfg.get("toc_parser"))
        if parser:
            try:
                arts = parser(_http_get(jcfg["toc_url"]))
                if arts:
                    print(f"  parse_toc: server-side RSS OK — {len(arts)} articles "
                          f"(skipped Chrome Beta)", flush=True)
                    return arts
                print("  parse_toc: server-side RSS returned 0 articles; "
                      "falling back to browser", flush=True)
            except Exception as e:
                print(f"  parse_toc: server-side RSS failed ({e!r}); "
                      f"falling back to browser", flush=True)
    # Server-side Crossref path (NDT/OUP, Kidney360/ASN): the publisher RSS /
    # issue pages are bot-blocked (404/403), so the TOC list comes from Crossref
    # journal-works for the ISSN, newest first, filtered to a rolling weekly
    # window (from-pub-date = issue_date - toc_window_days). OUP/ASN online-first
    # articles lack a clean issue boundary, so this is a "recent N days" feed,
    # not a numbered issue. Abstracts come from Crossref where deposited (~60%);
    # step-2a leaves them as-is. Fully server-side, no browser.
    if jcfg.get("toc_format") == "crossref":
        if _crossref is None:
            raise RuntimeError("crossref_enrich module required for toc_format=crossref")
        since = None
        if jcfg.get("toc_window_days") and issue_date:
            since = (datetime.strptime(issue_date, "%Y-%m-%d")
                     - timedelta(days=int(jcfg["toc_window_days"]))).strftime("%Y-%m-%d")
        arts = _crossref.parse_crossref_recent(
            jcfg["issn"], since_date=since, rows=int(jcfg.get("toc_rows", 60)),
            mailto=CROSSREF_MAILTO)
        print(f"  parse_toc: Crossref recent — {len(arts)} articles "
              f"(ISSN {jcfg['issn']}, since {since or 'n/a'})", flush=True)
        return arts
    # Server-side HTML path (bepress / static-HTML OA journals like JFDA): plain
    # urllib fetch + Python HTML parse, no browser session. Falls through to the
    # browser path on any failure (zero regression for browser-only journals).
    if jcfg.get("toc_format") == "html":
        parser = _HTML_SERVERSIDE_PARSERS.get(jcfg.get("toc_parser"))
        if parser:
            try:
                arts = parser(_http_get(jcfg["toc_url"]))
                if arts:
                    print(f"  parse_toc: server-side HTML OK — {len(arts)} articles "
                          f"(skipped Chrome Beta)", flush=True)
                    return arts
                print("  parse_toc: server-side HTML returned 0 articles; "
                      "falling back to browser", flush=True)
            except Exception as e:
                print(f"  parse_toc: server-side HTML failed ({e!r}); "
                      f"falling back to browser", flush=True)
    # Cookie + curl_cffi path: for subscriber journals carrying a `cookie_auth`
    # config, fetch the TOC HTML server-side with Chrome-impersonation + dumped
    # session cookies (via a helper module `_journal_fetcher_lib` placed in the
    # directory named by JOURNAL_FETCHER_LIB_DIR — the same Cloudflare-proof
    # stack the by-DOI fetcher uses) and parse it in pure Python. This makes a
    # scheduled parseToc immune to the live-tab Cloudflare 403 that occurs when
    # the parseToc XHR fires from a tab stuck on the CF challenge. ANY failure —
    # no helper dir, no cookie dump, expired cookies, CF wall, 0 articles —
    # falls through to the browser path below (zero regression). Disable with
    # BUNDLER_NO_COOKIE=1.
    cauth = jcfg.get("cookie_auth")
    _fetcher_dir = os.environ.get("JOURNAL_FETCHER_LIB_DIR", "")
    if cauth and _fetcher_dir and not os.environ.get("BUNDLER_NO_COOKIE"):
        py_parser = _COOKIE_TOC_PARSERS.get(cauth.get("parser"))
        if py_parser:
            try:
                _lib_dir = str(Path(_fetcher_dir).expanduser())
                if _lib_dir not in sys.path:
                    sys.path.insert(0, _lib_dir)
                import _journal_fetcher_lib as _jfl
                raw_cookies, ck_src = _jfl.load_latest_cookies(cauth["domain_key"])
                sess = _jfl.build_session(raw_cookies, referer=jcfg["toc_url"])
                _, toc_html = _jfl.fetch_html(sess, jcfg["toc_url"])
                arts = py_parser(toc_html)
                if arts:
                    print(f"  parse_toc: cookie+curl_cffi OK — {len(arts)} "
                          f"articles (no browser; cookies {ck_src.name})",
                          flush=True)
                    # The cookie path used NO browser tab, but per-article
                    # extract_article (NEJM wiki_ingest_all) + PDF still run via
                    # the browser (NEJM full text/PDF are real-browser-finger-
                    # printed; curl_cffi can't reliably fetch them). Actively
                    # CLEAR a browser tab now so the extract phase doesn't
                    # inherit the 403 we just dodged at the TOC.
                    if jcfg.get("wiki_ingest_all") or jcfg.get("fetch_pdf"):
                        try:
                            if not ensure_cleared_tab(jcfg["browser"], jcfg["toc_url"]):
                                print("  parse_toc: WARN browser tab not CF-cleared "
                                      "after wait; extract phase may degrade",
                                      flush=True)
                        except Exception:
                            pass
                    return arts
                print("  parse_toc: cookie path returned 0 articles; "
                      "falling back to browser", flush=True)
            except SystemExit:
                # load_latest_cookies sys.exit(3) when no dump exists — treat as
                # "no cookies available, use browser", NOT a fatal bundler exit.
                print("  parse_toc: no cookie dump found; "
                      "falling back to browser", flush=True)
            except Exception as e:
                print(f"  parse_toc: cookie path failed ({e!r}); "
                      f"falling back to browser", flush=True)

    # Ensure session tab is open before parseToc — session_seed_url overrides
    # toc_url when present (e.g. Science RSS URL would download .xml not
    # establish HTML session; use journal home page instead).
    seed_url = jcfg.get("session_seed_url") or jcfg["toc_url"]
    ensure_tab_open(jcfg["browser"], seed_url, wait_seconds=jcfg.get("session_seed_wait", 8))

    def _run_parsetoc() -> dict:
        payload = osa_run(
            jcfg["browser"],
            urldomain(jcfg["toc_url"]),
            "parseToc",
            {
                "toc_url": jcfg["toc_url"],
                "parser": jcfg["toc_parser"],
                "issue_date": issue_date,
            },
        )
        return json.loads(payload)

    data = _run_parsetoc()
    if "error" in data:
        # ensure_tab_open() matches only the domain, so a stale/backgrounded
        # journal tab (home page or a prior issue) makes the parseToc XHR fire
        # from a tab without fresh Cloudflare clearance for the exact
        # currentissue path -> http 403 / "Just a moment" challenge (same class
        # as the 2026-06-04 NEJM video-tab RCA: domain match != right tab).
        # Force-navigate the EXACT toc_url to land fresh CF clearance, wait, and
        # retry parseToc once. Success path is untouched (zero regression).
        err = str(data.get("error", "")).lower()
        if any(s in err for s in ("403", "challenge", "just a moment", "fetch")):
            print(f"  parse_toc: {data} — force-navigating exact toc_url + retry once",
                  flush=True)
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'tell application "{jcfg["browser"]}"\n'
                     f'  activate\n'
                     f'  open location "{jcfg["toc_url"]}"\n'
                     f'end tell'],
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                pass
            time.sleep(jcfg.get("cf_clearance_wait", 14))
            data = _run_parsetoc()
    if "error" in data:
        raise RuntimeError(f"parseToc failed: {data}")
    return data["articles"]


_JUNK_BODY_URL = re.compile(
    r'(?:adVert|/templates/|/_marlin/|/style2/|/_style\d|/images/bg_|doubleclick|/banner|sponsor)',
    re.I)


def strip_body_template_junk(md: str) -> str:
    """Remove publisher page-template chrome that the in-browser extractor pulls
    into body_md when the real article body is paywalled (RCA 2026-06-26: Lancet
    paywalled pages yielded a body of just '## Article metrics' + a bg_adVert.gif
    ad figure, which `grep '^## Article'` then double-counted as phantom articles).
    Drops: (a) template/ad figure blocks (**[Figure]** + a junk-domain URL line),
    (b) bare junk-URL lines, (c) a '## Metrics' / '## Article metrics' section up to
    the next heading. Leaves real article bodies untouched."""
    if not md:
        return md
    lines = md.split("\n")
    out: list = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r'^#{2,4}\s+(article\s+)?metrics\b', ln, re.I):
            i += 1
            while i < len(lines) and not re.match(r'^#{1,4}\s+\S', lines[i]):
                i += 1
            continue
        if ln.strip().startswith("**[Figure]**"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if _JUNK_BODY_URL.search(ln) or _JUNK_BODY_URL.search(nxt):
                i += 2 if (i + 1 < len(lines) and nxt.strip().startswith("<")) else 1
                continue
        if ln.strip().startswith("<") and _JUNK_BODY_URL.search(ln):
            i += 1
            continue
        out.append(ln)
        i += 1
    return re.sub(r'\n{3,}', '\n\n', "\n".join(out)).strip()


def extract_article(jcfg: dict, article_url: str, discover_media: bool = False) -> dict:
    payload = osa_run(
        jcfg["browser"],
        urldomain(jcfg["toc_url"]),
        "extractArticle",
        {
            "url": article_url,
            "title_selectors": jcfg.get("title_selectors", "main h1, article h1"),
            "abstract_selectors": jcfg["abstract_selectors"],
            "body_selectors": jcfg["body_selectors"],
            "discover_media": discover_media,
        },
    )
    return json.loads(payload)


def fetch_pdf_base64(jcfg: dict, pdf_url: str) -> dict:
    payload = osa_run(
        jcfg["browser"],
        urldomain(jcfg["toc_url"]),
        "fetchPdfBase64",
        {"url": pdf_url},
    )
    return json.loads(payload)


def fetch_binary_base64(jcfg: dict, url: str) -> dict:
    payload = osa_run(
        jcfg["browser"],
        urldomain(jcfg["toc_url"]),
        "fetchBinaryBase64",
        {"url": url},
    )
    return json.loads(payload)


def _osa_eval_in_tab(browser: str, tab_match: str, js: str) -> str:
    """Run a JS expression in an existing Chrome tab via osascript `execute javascript`.
    Returns the JS expression's value (raw stdout).

    Helper for the NEJM video resolution chain (Task #9). Distinct from osa_run()
    which dispatches into the bundled extractor.js — here we eval ad-hoc JS in
    whatever tab matches `tab_match` (typically the article's /doi/full/<doi>).
    """
    js_esc = js.replace("\\", "\\\\").replace('"', '\\"')
    applescript = (
        f'tell application "{browser}"\n'
        f'  set targetTab to missing value\n'
        f'  repeat with w in windows\n'
        f'    repeat with t in tabs of w\n'
        f'      if (URL of t as string) contains "{tab_match}" then\n'
        f'        set targetTab to t\n'
        f'        exit repeat\n'
        f'      end if\n'
        f'    end repeat\n'
        f'    if targetTab is not missing value then exit repeat\n'
        f'  end repeat\n'
        f'  if targetTab is missing value then return "NO_TAB"\n'
        f'  return (execute targetTab javascript "{js_esc}")\n'
        f'end tell'
    )
    r = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def resolve_nejm_video_ref(jcfg: dict, article_url: str, nejmdo_ref: str, ajaxurl: str) -> dict:
    """Resolve a single NEJM video reference to a downloaded 720w mp4.

    Pipeline (doc: README §"NEJM video resolution"):
      1. ensure article tab open in Chrome Beta (subscriber session cookies needed
         so the /do/ ajax endpoint passes Cloudflare).
      2. in-page `fetch(ajaxurl, credentials:'include', X-Requested-With:'XMLHttpRequest')`
         (sync XHR hits Cloudflare 403; async fetch from page context passes).
      3. parse JSON `{hasAccess:true, html:'<media-player-app mediaID="X" player="vrt|qt">'}`.
         text-only Research Summary returns `{hasAccess:true}` without `html` → not video.
      4. JW Platform API `https://content.jwplatform.com/v2/media/<mediaID>` (public,
         stdlib OK, no Cloudflare).
      5. pick 720w mp4 (label=="720w" or width==720), fallback first mp4.
      6. download via stdlib urllib (JW CDN is content.jwplatform.com — public).

    Returns dict with {media_id, mp4_url, duration, player_hint, title, _mp4_bytes}.
    Returns empty dict on any failure (caller WARNs and moves on).

    Caveat: figure-only animations (no narration) come back as silent <30s mp4 —
    MacWhisper will FAIL "No audio track found" downstream; the bundle keeps the
    mp4 as a figure asset. Examples: NEJMdo008410 (Zimmermann 12s), NEJMdo008475
    (Silent Aspiration 17s).
    """
    if not nejmdo_ref or not ajaxurl:
        return {}
    # Force-activate the SPECIFIC article tab. ensure_tab_open() only matches the
    # DOMAIN, so with the TOC (or another nejm.org tab) open it no-ops and never
    # brings THIS article forward — and a credentialed AJAX fetch fired from a
    # backgrounded/discarded nejm tab returns the Cloudflare "Just a moment"
    # challenge instead of the player JSON (confirmed 2026-06-04, the real cron
    # failure mode). `open location` focuses+activates the exact article tab
    # (Chrome dedups by URL), waking it so the fetch carries cf_clearance — this
    # is what dev/download-nejm-video's session resolver does.
    try:
        subprocess.run(["osascript", "-e",
            f'tell application "{jcfg["browser"]}"\n  activate\n  open location "{article_url}"\nend tell'],
            capture_output=True, text=True, timeout=20)
        time.sleep(2)
    except Exception as e:
        print(f"      WARN activate article tab failed: {e}")

    tab_match = urldomain(article_url) + article_url.split(urldomain(article_url), 1)[1].split("?", 1)[0]
    # Use article DOI fragment as tab matcher
    doi_suffix = article_url.rsplit("/", 1)[-1]

    # Wait for Cloudflare clearance before the credentialed AJAX fetch. A fetch
    # fired before the article tab clears returns the "Just a moment..." challenge
    # HTML instead of the player JSON (the 2026-06-04 timing failure — the regex
    # fix alone was necessary but not sufficient). Poll readyState + title.
    for _ in range(20):
        chk = _osa_eval_in_tab(jcfg["browser"], doi_suffix,
            "(function(){return (document.readyState||'')+'|'+(document.title||'');})()")
        if chk and chk != "NO_TAB" and "Just a moment" not in chk and chk.split("|", 1)[0] == "complete":
            break
        time.sleep(1)

    # 1+2. Kick async fetch (window.__nejm_vr=null first, then assigns on resolve).
    # Robust extractor: no-throw JSON parse, media_id from html||raw with BOTH the
    # lowercase iframe pattern (media_id=X) and legacy camelCase (mediaID="X"),
    # plus a challenge flag so we re-kick after Cloudflare settles.
    kick_js = (
        "(function(){window.__nejm_vr=null;"
        f"fetch('{ajaxurl}',{{credentials:'include',headers:{{'X-Requested-With':'XMLHttpRequest','Accept':'text/html,*/*'}}}})"
        ".then(function(r){return r.text();})"
        ".then(function(t){try{"
        "var ch=t.indexOf('Just a moment')>-1||t.indexOf('cf-browser-verification')>-1||t.indexOf('challenge-platform')>-1;"
        "var j=null;try{j=JSON.parse(t);}catch(e){}"
        "var html=(j&&j.html)||'';var s=html||t;"
        "var m=s.match(/media_id=([A-Za-z0-9]+)/)||s.match(/mediaID=\"([A-Za-z0-9]+)\"/);"
        "var pm=s.match(/player=\"?([a-z]+)\"?/);"
        "window.__nejm_vr={ok:true,challenge:ch,media_id:m?m[1]:null,player:pm?pm[1]:null,bytes:t.length,has_html:html.length>0};}"
        "catch(e){window.__nejm_vr={ok:false,error:String(e)};}})"
        ".catch(function(e){window.__nejm_vr={ok:false,error:String(e)};});"
        "return 'KICKED';})()"
    )

    # 3. Kick + poll, retrying if a Cloudflare challenge slips through.
    media_id = None
    player_hint = None
    for _attempt in range(4):
        _osa_eval_in_tab(jcfg["browser"], doi_suffix, kick_js)
        d = None
        for _ in range(12):  # up to ~12s
            time.sleep(1)
            out = _osa_eval_in_tab(jcfg["browser"], doi_suffix, "(function(){return window.__nejm_vr?JSON.stringify(window.__nejm_vr):'PENDING';})()")
            if out and out != "PENDING" and out != "NO_TAB":
                try:
                    d = json.loads(out)
                except Exception:
                    d = None
                break
        if d and d.get("ok") and d.get("media_id"):
            media_id = d["media_id"]
            player_hint = d.get("player")
            break
        if d and d.get("challenge"):
            time.sleep(4)  # let Cloudflare finish its JS challenge, then re-kick
            continue
        break  # real non-video response (e.g. text Research Summary {hasAccess:true})
    if not media_id:
        return {"error": "no media_id", "nejmdo": nejmdo_ref, "player_hint": player_hint}

    # 4-5. Query JW Platform
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"https://content.jwplatform.com/v2/media/{media_id}", timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:
        return {"error": f"jw lookup failed: {e}", "media_id": media_id}

    pl = (data.get("playlist") or [{}])[0]
    mp4_url = None
    for s in pl.get("sources", []):
        if s.get("type") == "video/mp4" and (s.get("label") == "720w" or s.get("width") == 720):
            mp4_url = s.get("file")
            break
    if not mp4_url:
        for s in pl.get("sources", []):
            if s.get("type") == "video/mp4":
                mp4_url = s.get("file")
                break
    if not mp4_url:
        return {"error": "no mp4 source", "media_id": media_id}

    # 6. Download
    try:
        import urllib.request as _ur
        req = _ur.Request(mp4_url, headers={"User-Agent": "Mozilla/5.0 nejm-bundler"})
        with _ur.urlopen(req, timeout=120) as resp:
            mp4_bytes = resp.read()
    except Exception as e:
        return {"error": f"mp4 download failed: {e}", "media_id": media_id, "mp4_url": mp4_url}

    return {
        "media_id": media_id,
        "mp4_url": mp4_url,
        "title": pl.get("title"),
        "duration": pl.get("duration"),
        "player_hint": player_hint or "vrt",
        "_mp4_bytes": mp4_bytes,
    }


def discover_issue_audio(jcfg: dict, yymmdd: str) -> dict:
    payload = osa_run(
        jcfg["browser"],
        urldomain(jcfg["toc_url"]),
        "discoverIssueAudio",
        {"yymmdd": yymmdd},
    )
    return json.loads(payload)


def urldomain(url: str) -> str:
    """Domain substring suitable for AppleScript tab-URL matching."""
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url


def citation_key(jcfg: dict, doi: str) -> str:
    """`10.7326/ANNALS-25-03691` -> `annals-25-03691`."""
    suffix = doi.split("/", 1)[1] if "/" in doi else doi
    return re.sub(r"[^a-z0-9_-]", "-", suffix.lower())


def md_quote(s: str) -> str:
    """Escape stray chars for inline citation in mega md."""
    return s.replace("\n", " ").replace("|", "/").strip()


def safe_filename(name: str, fallback: str) -> str:
    """Sanitize a filename derived from URL — keep alnum + . _ -."""
    name = (name or "").strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "", name) or fallback
    return name[:100]


# Binary staging policy: all bundler-fetched binary lands first in the inbox
# ($JOURNAL_INBOX, default ~/Downloads) so the operator can see what was
# processed. Then a stage-2 promote moves the binary into the topic store under
# a topic-only folder; deterministic types (NEJMcps / NEJMcpc →
# research_method/clinical_d_d) are auto-promoted inline; other types stay in
# the inbox awaiting manual classification. Stage-3 archive moves the inbox
# copy to <inbox>/_archive/<orig>.duplicate-of-<citation_key>.<ext> after a
# successful promote.
DOWNLOADS_INBOX = Path(os.environ.get("JOURNAL_INBOX", str(Path.home() / "Downloads")))
DOWNLOADS_ARCHIVE = DOWNLOADS_INBOX / "_archive"
JOURNAL_TOPIC_STORE_ROOT = os.environ.get(
    "JOURNAL_TOPIC_STORE_ROOT", str(Path.home() / "journal-corpus" / "_topics"))
DEVICE = (os.environ.get("DEVICE") or socket.gethostname().split(".")[0]).lower()
SPACE_CONSTRAINED_HOSTS = set(
    filter(None, os.environ.get("JOURNAL_SPACE_CONSTRAINED_HOSTS", "").split(",")))


def shlex_quote(s: str) -> str:
    return shlex.quote(s)


def stage1_inbox_dir(journal_key: str, issue_date: str) -> Path:
    """Per-journal-issue subfolder under <inbox>/journal-toc/.

    Bundler artifacts (mega md + every binary) land in a per-issue subfolder so
    the inbox root stays uncluttered when multiple journals run in the same
    window.
    """
    return DOWNLOADS_INBOX / "journal-toc" / journal_key / issue_date


def stage1_inbox_path(journal_key: str, issue_date: str, citation_key: str, filename: str) -> Path:
    """e.g. <inbox>/journal-toc/nejm/2026-05-07/nejm-2026-05-07_nejmoa2515704_source.pdf"""
    name = f"{journal_key}-{issue_date}_{citation_key}_{filename}"
    return stage1_inbox_dir(journal_key, issue_date) / name


def write_binary_to_inbox(journal_key: str, issue_date: str, citation_key: str,
                          filename: str, raw_bytes: bytes) -> dict:
    """Stage 1: drop binary into the inbox journal-toc subfolder."""
    inbox_path = stage1_inbox_path(journal_key, issue_date, citation_key, filename)
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_bytes(raw_bytes)
    sha = hashlib.sha256(raw_bytes).hexdigest()
    return {
        "placement": "inbox",
        "inbox_path": str(inbox_path),
        "filename": filename,
        "bytes": len(raw_bytes),
        "sha256": sha,
    }


def nejm_topic_for(art: dict) -> tuple | None:
    """Return (topic_path, slug) for NEJM article types that map
    deterministically; otherwise None (binary stays in inbox awaiting
    classification).

    Deterministic auto-route: the NEJM article
    types that ARE the heuristic-diagnosis genre route to
    research_method/clinical_d_d/{citation_key}/:

      - NEJMcps  Clinical Problem Solving — clinician walks a case unfolding
                 piece by piece, demonstrating diagnostic reasoning.
      - NEJMcpc  Case Records of the Massachusetts General Hospital — the
                 classic CPC (clinicopathological conference): case
                 presentation → discussant differential → pathology resolves.

    NOT auto-routed:

      - NEJMclde Clinical Decisions — most pieces pit two experts on
                 opposing treatment recommendations rather than walking
                 through diagnostic reasoning. Fails the heuristic-diagnosis
                 qualifying criterion. Per-issue judgement decides whether
                 a given Clinical Decisions piece actually fits clinical_d_d.
      - everything else (NEJMoa research, NEJMra reviews, NEJMp perspectives,
                 NEJMicm images, NEJMe editorials, NEJMc correspondence) —
                 topic depends on content; stays in inbox awaiting
                 manual classification.
    """
    type_code = (art.get("type_code") or "").lower()
    if type_code in ("cps", "cpc"):
        ck = art.get("citation_key") or ""
        if ck:
            return ("research_method/clinical_d_d", ck)
    return None


def promote_inbox_to_topic(inbox_info: dict, topic_path: str, slug: str,
                            citation_key: str) -> dict:
    """Stage 2 + 3: move binary from the inbox into the topic-store folder,
    then archive the inbox copy. When JOURNAL_TOPIC_STORE_HOST is set the write
    goes to that peer host over ssh; otherwise it is written to the local topic
    store (JOURNAL_TOPIC_STORE_ROOT)."""
    inbox_path = Path(inbox_info["inbox_path"])
    if not inbox_path.exists():
        return {"placement": inbox_info["placement"], "error": "inbox_path missing", "inbox_info": inbox_info}
    raw_bytes = inbox_path.read_bytes()
    # Filename in topic folder = original filename (without journal/date/ck prefix).
    target_name = inbox_info["filename"]
    remote = f"{JOURNAL_TOPIC_STORE_ROOT}/{topic_path}/{slug}/{target_name}"
    host = os.environ.get("JOURNAL_TOPIC_STORE_HOST", "")
    if host:
        remote_dir = remote.rsplit("/", 1)[0]
        cmd = f"mkdir -p {shlex_quote(remote_dir)} && cat > {shlex_quote(remote)}"
        r = subprocess.run(["ssh", host, cmd], input=raw_bytes, capture_output=True, timeout=120)
        if r.returncode != 0:
            return {"placement": "inbox", "error": f"ssh-cat rc={r.returncode}: {r.stderr.decode(errors='replace')[:200]}", "inbox_info": inbox_info}
    else:
        dest = Path(remote).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw_bytes)
    # Stage 3: archive inbox copy
    DOWNLOADS_ARCHIVE.mkdir(parents=True, exist_ok=True)
    archived_name = f"{inbox_path.stem}.duplicate-of-{citation_key}{inbox_path.suffix}"
    inbox_path.rename(DOWNLOADS_ARCHIVE / archived_name)
    return {
        "placement": "topic_store",
        "topic_path": topic_path,
        "slug": slug,
        "filename": target_name,
        "dest_uri": f"{host}:{remote}" if host else remote,
        "bytes": inbox_info["bytes"],
        "sha256": inbox_info["sha256"],
        "inbox_archived_to": str(DOWNLOADS_ARCHIVE / archived_name),
    }


def _oa_repo_only(a: dict) -> bool:
    """Green OA: free full text lives only in a repository (PMC / institutional);
    the publisher page itself is paywalled. Must NOT be displayed as plain "OA"
    (bug report #6 2026-07-22 — JAMA RCT publisher links hit the paywall)."""
    return bool(a.get("is_oa")) and (
        a.get("oa_status") == "green" or a.get("oa_host_type") == "repository")


def build_mega_md(journal_key: str, jcfg: dict, issue_date: str, articles: list) -> str:
    green_count = sum(1 for a in articles if _oa_repo_only(a))
    oa_count = sum(1 for a in articles if a.get("is_oa")) - green_count
    has_bodies = any((a.get("body_md") or "").strip() for a in articles)
    n_abs = sum(1 for a in articles if (a.get("abstract") or "").strip())
    n_cr = sum(1 for a in articles if a.get("abstract_source") in ("crossref", "europepmc"))
    if has_bodies:
        fidelity = [
            "fidelity_notes: |",
            "  Whole-issue full-text bundle. abstract + body markdown for every",
            "  article in the TOC, fetched via subscriber-session sync XHR with",
            "  per-publisher selector union. Used as: (1) GPT input for journal-",
            "  reading-guide drafting, (2) source for designated per-article raw",
            "  ingest into topic-note folders.",
        ]
    else:
        fidelity = [
            "fidelity_notes: |",
            "  TOC + abstract bundle (no article body). title + section +",
            f"  article-type + DOI for every TOC entry; abstract present for",
            f"  {n_abs}/{len(articles)} articles (browser/RSS where available, plus",
            "  server-side Crossref by-DOI metadata — see counts below). NO article",
            "  body was fetched. When writing the reading guide, comment from",
            "  titles/sections + the abstract text only; do NOT infer methods/",
            "  results not shown here. Articles with no abstract here (Books/",
            "  Letters/front-matter) carry none at the publisher either. Per-article",
            "  full text is a separate Step-2 ingest (--dois --force-ingest-all).",
        ]
    if n_cr:
        fidelity.append(
            f"  ({n_cr}/{len(articles)} abstracts filled from Crossref DOI metadata; "
            "these are abstract-only — no body.)")
    fm = [
        "---",
        "type: raw_of_raw",
        f"journal: {jcfg['name']}",
        f"journal_slug: {journal_key}",
        f"issue_date: {issue_date}",
        f"captured_at: {now_iso()}",
        f"articles_total: {len(articles)}",
        f"oa_count: {oa_count}",
        f"green_oa_count: {green_count}",
        f"extracted_via: journal-bundler-v0.1.0 (sync-xhr + simpleHtmlToMarkdown)",
        *fidelity,
        "---",
        "",
        f"# {jcfg['name']} — {issue_date} 全期 raw bundle",
        "",
        (f"{len(articles)} 篇文章，{oa_count} 篇 open access"
         + (f"，另 {green_count} 篇 green OA（出版社頁付費，免費全文僅在 repository）。"
            if green_count else "。")),
        "",
    ]
    for i, a in enumerate(articles, 1):
        fm.append("---")
        fm.append("")
        fm.append(f"## Article {i} — {md_quote(a.get('title', '(no title)'))}")
        fm.append("")
        fm.append(f"- **DOI**: [{a['doi']}](https://doi.org/{a['doi']})")
        fm.append(f"- **Section**: {a.get('section') or '(unspecified)'}")
        if a.get("subtype"):
            fm.append(f"- **Subtype**: {md_quote(a['subtype'])}")
        if _oa_repo_only(a):
            fm.append("- **OA**: green（出版社頁付費；免費全文僅在 repository）")
            if a.get("oa_free_url"):
                fm.append(f"- **OA free link**: <{a['oa_free_url']}>")
        else:
            fm.append(f"- **OA**: {'yes' if a.get('is_oa') else 'no'}")
        fm.append(f"- **Article URL**: <{a['article_url']}>")
        fm.append(f"- **PDF URL**: <{a['pdf_url']}>")
        if a.get("authors"):
            fm.append(f"- **Authors**: {md_quote(a['authors'])[:600]}")
        if a.get("pub_date"):
            fm.append(f"- **Pub date**: {a['pub_date']}")
        fm.append("")
        fm.append("### Listing briefing")
        fm.append("")
        if a.get("listing_brief"):
            fm.append(a["listing_brief"])
        else:
            fm.append("*(no listing-page briefing — wrapper, letter, or section without TOC abstract)*")
        fm.append("")
        fm.append("### Abstract")
        fm.append("")
        if a.get("abstract"):
            fm.append(a["abstract"])
        else:
            fm.append("*(no abstract extracted from article page)*")
        fm.append("")
        fm.append("### Body")
        fm.append("")
        if a.get("body_md"):
            fm.append(a["body_md"])
        else:
            fm.append("*(no body extracted — paywall or DOM mismatch)*")
        fm.append("")
        if a.get("figures"):
            fm.append("### Figures")
            fm.append("")
            for fi, fig in enumerate(a["figures"], 1):
                fm.append(f"- [{fi}] <{fig['url']}>" + (f" — {md_quote(fig['caption'])[:200]}" if fig.get('caption') else ""))
            fm.append("")
    return "\n".join(fm)


def lookup_existing_payload_path(doi: str | None = None, url: str | None = None) -> Path | None:
    """Auto-dedup: if a previous ingest of this DOI / canonical URL already has a
    raw_md_path in PG, return its folder so re-runs reuse the canonical location
    instead of creating a fresh dated copy.

    Use cases:
      - journal-toc: BMJ /content/current 302-redirects to a stable issue across
        days; same DOIs reappear → dedup hits, no duplicate folder.
      - webpage / DynaMed snapshot / paywalled url-keyed source: pass url= to
        match raw_source_metadata via identifiers->>'url' or url:* source_uid.

    Best-effort: any PG/connectivity error returns None so dedup never blocks ingest."""
    if not doi and not url:
        return None
    psql = PSQL_BIN
    if not Path(psql).exists():
        psql = "psql"
    pg_host = PGHOST

    where_clauses = []
    if doi:
        where_clauses.append(f"doi = '{doi.lower().strip()}'")
    if url:
        u = url.strip().replace("'", "''")
        where_clauses.append(
            f"identifiers->>'url' = '{u}' OR source_uid = 'url:{u}'"
        )
    sql = (
        "SELECT raw_md_path FROM wiki_raw.raw_source_metadata "
        f"WHERE ({' OR '.join(where_clauses)}) AND raw_md_path IS NOT NULL "
        "AND sync_deleted_at IS NULL ORDER BY updated_at DESC NULLS LAST LIMIT 1;"
    )
    try:
        r = subprocess.run([psql, "-h", pg_host, "-U", PGUSER, "-d", PGDATABASE, "-tA", "-c", sql],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        rel_path = lines[0]
        rp = Path(rel_path)
        full = rp if rp.is_absolute() else Path.home() / rel_path
        if full.name == "raw.md":
            return full.parent
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def write_per_article_payload(journal_key: str, jcfg: dict, issue_date: str, art: dict, pdf_b64: str | None) -> Path:
    ck = citation_key(jcfg, art["doi"])
    # DOI-based dedup: reuse existing canonical folder if this DOI was already ingested
    # under a different issue_date (e.g. /content/current redirected to same issue across runs)
    existing = lookup_existing_payload_path(doi=art["doi"], url=art.get("article_url"))
    default_folder = WIKI_RAW / "_journal_toc" / journal_key / issue_date / "articles" / ck
    deduped_from_issue_date = None
    if existing and existing.exists():
        folder = existing
        # Recover canonical issue_date from existing folder structure. Post-
        # 2026-05-14 layout: ".../journal_toc/bmj/2026-05-02/articles/bmj-s823"
        # → parent='articles', grandparent='2026-05-02'. Pre-migration layout
        # (kept for back-compat with any unmigrated legacy paths): grandparent
        # was "{date}_articles".
        gp = folder.parent.parent.name if folder.parent.name == "articles" else folder.parent.name
        m = re.match(r'^(\d{4}-\d{2}-\d{2})(?:_articles)?$', gp)
        if m:
            deduped_from_issue_date = m.group(1)
            issue_date = deduped_from_issue_date  # frontmatter+manifest reflect canonical issue_date
    else:
        folder = default_folder
    folder.mkdir(parents=True, exist_ok=True)

    title = art.get("title", "")
    body = art.get("body_md", "")
    abstract = art.get("abstract", "")

    listing_brief = art.get("listing_brief") or ""
    raw_lines = [
        "---",
        "type: raw",
        f"citation_key: {ck}",
        f"doi: {art['doi']}",
        f"uid: doi:{art['doi']}",
        "source_type: journal_article",
        f"journal: {jcfg['name']}",
        f"journal_slug: {journal_key}",
        f"title: {json.dumps(title)}",
        f"section: {json.dumps(art.get('section', ''))}",
        f"subtype: {json.dumps(art.get('subtype', ''))}",
        f"is_oa: {str(art.get('is_oa', False)).lower()}",
        *([f"oa_status: {art['oa_status']}"] if art.get("oa_status") else []),
        *([f"oa_free_url: {art['oa_free_url']}"] if art.get("oa_free_url") else []),
        f"article_url: {art['article_url']}",
        f"pdf_url: {art['pdf_url']}",
        f"issue_date: {issue_date}",
        f"captured_date: {today_str()}",
        f"extracted_via: journal-bundler-v0.1.0",
        "---",
        "",
        f"# {title}",
        "",
        f"**Source**: {jcfg['name']}, {art.get('section', '(unspecified)')}, issue {issue_date}",
        "",
        f"**Article URL**: <{art['article_url']}>",
        "",
        "## Listing briefing",
        "",
        listing_brief if listing_brief else "*(no listing-page briefing)*",
        "",
        "## Abstract",
        "",
        abstract if abstract else "*(no abstract extracted)*",
        "",
        "## Body",
        "",
        body if body else "*(no body extracted)*",
        "",
    ]
    if art.get("figures"):
        raw_lines.append("## Figures (URLs)")
        raw_lines.append("")
        for fi, fig in enumerate(art["figures"], 1):
            raw_lines.append(f"- [{fi}] <{fig['url']}>" + (f" — {md_quote(fig['caption'])[:200]}" if fig.get('caption') else ""))
        raw_lines.append("")

    # raw.md (text) lands in the per-issue staging folder for every article.
    # When the article auto-promotes (NEJMcps/NEJMcpc), a second copy lands in
    # the topic-store folder alongside the binary.
    (folder / "raw.md").write_text("\n".join(raw_lines), encoding="utf-8")
    art["citation_key"] = ck  # for nejm_topic_for() classifier
    promote = nejm_topic_for(art)

    # Binary writes: stage 1 always = the inbox ($JOURNAL_INBOX) so the operator
    # sees what's processed. Stage 2 promote = copy from inbox to the topic-store
    # folder for deterministically-classified types (NEJMcps/NEJMcpc →
    # clinical_d_d). Stage 3 archive = inbox copy moves to <inbox>/_archive/
    # after a successful promote.
    pdf_meta = None
    if pdf_b64:
        raw = base64.b64decode(pdf_b64)
        info = write_binary_to_inbox(journal_key, issue_date, ck, "source.pdf", raw)
        pdf_meta = info
        if promote:
            promoted = promote_inbox_to_topic(info, promote[0], promote[1], ck)
            pdf_meta = {**info, **promoted}

    suppl_files = []
    audio_files = []
    video_files = []
    for s in (art.get("media") or {}).get("supplementary", []) or []:
        try:
            r = fetch_binary_base64(jcfg, s["url"])
            if not r.get("b64"):
                print(f"    WARN: suppl fetch empty for {s['url']}: {r.get('error')}")
                continue
            raw_bytes = base64.b64decode(r["b64"])
            fn = "supplementary_" + safe_filename(s.get("kind", "supplementary"), "supplementary") + ".pdf"
            info = write_binary_to_inbox(journal_key, issue_date, ck, fn, raw_bytes)
            entry = {
                "filename": fn,
                "kind": s.get("kind"),
                "source_url": s["url"],
                "bytes": info["bytes"],
                "sha256": info["sha256"],
                "placement": info["placement"],
                "inbox_path": info["inbox_path"],
                "caption": s.get("caption"),
            }
            if promote:
                promoted = promote_inbox_to_topic(info, promote[0], promote[1], ck)
                entry.update({k: v for k, v in promoted.items() if k != "filename"})
            suppl_files.append(entry)
        except Exception as e:
            print(f"    WARN: suppl fetch failed for {s['url']}: {e}")

    for media_kind, media_list, target_list in (
        ("audio", (art.get("media") or {}).get("audio", []) or [], audio_files),
        ("video", (art.get("media") or {}).get("video", []) or [], video_files),
    ):
        for m in media_list:
            try:
                r = fetch_binary_base64(jcfg, m["url"])
                if not r.get("b64"):
                    print(f"    WARN: {media_kind} fetch empty for {m['url']}: {r.get('error')}")
                    continue
                raw_bytes = base64.b64decode(r["b64"])
                src_name = m.get("filename") or m["url"].split("/")[-1].split("?")[0]
                stem, _, ext = src_name.rpartition(".")
                if not stem:
                    stem, ext = src_name, ("mp3" if media_kind == "audio" else "mp4")
                fn = f"{media_kind}_{safe_filename(stem, media_kind)}.{ext.lower()}"
                info = write_binary_to_inbox(journal_key, issue_date, ck, fn, raw_bytes)
                entry = {
                    "filename": fn,
                    "kind": m.get("kind", media_kind),
                    "source_url": m["url"],
                    "bytes": info["bytes"],
                    "sha256": info["sha256"],
                    "placement": info["placement"],
                    "inbox_path": info["inbox_path"],
                    "caption": m.get("caption"),
                }
                if promote:
                    promoted = promote_inbox_to_topic(info, promote[0], promote[1], ck)
                    entry.update({k: v for k, v in promoted.items() if k != "filename"})
                target_list.append(entry)
            except Exception as e:
                print(f"    WARN: {media_kind} fetch failed for {m['url']}: {e}")

    # NEJM Double Take / Quick Take video resolution (Task #9 2026-05-28).
    # extractor.js discoverArticleMediaFromHtml emits media.video_refs[] for
    # `.component-video[data-ajaxurl]` (Double Take, player=vrt) +
    # `.nejm-research-summary a[href="/do/NEJMdoNNN/full/"]` (Quick Take,
    # player=qt). Resolution chain (proven manually 2026-05-28 across current
    # + 5 backfill issues): article tab → in-page `fetch(ajaxurl, credentials:include)`
    # → JSON `{hasAccess:true, html:'<media-player-app mediaID="X">'}` →
    # JW Platform `https://content.jwplatform.com/v2/media/<id>` (public,
    # stdlib OK, bypasses NEJM Cloudflare) → 720w mp4. Doc: README
    # §"NEJM video resolution".
    if jcfg.get("name") == "New England Journal of Medicine":
        try:
            video_refs = (art.get("media") or {}).get("video_refs", []) or []
            for vref in video_refs:
                _media = resolve_nejm_video_ref(
                    jcfg=jcfg,
                    article_url=art["article_url"],
                    nejmdo_ref=vref.get("nejmdo", ""),
                    ajaxurl=vref.get("ajaxurl", ""),
                )
                if not _media or not _media.get("mp4_url"):
                    print(f"    WARN: video_refs unresolved for {vref.get('nejmdo')}: {_media}")
                    continue
                mp4_bytes = _media.pop("_mp4_bytes", b"")
                fn = f"video_{safe_filename(vref['nejmdo'].replace('10.1056/',''), 'video')}_720w.mp4"
                info = write_binary_to_inbox(journal_key, issue_date, ck, fn, mp4_bytes)
                entry = {
                    "filename": fn,
                    "kind": _media.get("player_hint", "video"),
                    "source_url": _media.get("mp4_url"),
                    "media_id": _media.get("media_id"),
                    "nejmdo": vref.get("nejmdo"),
                    "duration": _media.get("duration"),
                    "bytes": info["bytes"],
                    "sha256": info["sha256"],
                    "placement": info["placement"],
                    "inbox_path": info["inbox_path"],
                }
                if promote:
                    promoted = promote_inbox_to_topic(info, promote[0], promote[1], ck)
                    entry.update({k: v for k, v in promoted.items() if k != "filename"})
                video_files.append(entry)
        except Exception as e:
            print(f"    WARN: nejm video_refs resolution failed: {e}")

    # If auto-promote ran, also copy the raw.md into the topic-store folder so
    # {topic_path}/{slug}/raw.md is co-located with the binary. Staging copy
    # stays for cross-issue archive index.
    if promote and (pdf_meta and pdf_meta.get("placement") == "topic_store"):
        topic_path, slug = promote
        remote_raw = f"{JOURNAL_TOPIC_STORE_ROOT}/{topic_path}/{slug}/raw.md"
        host = os.environ.get("JOURNAL_TOPIC_STORE_HOST", "")
        try:
            if host:
                remote_dir = remote_raw.rsplit("/", 1)[0]
                cmd = f"mkdir -p {shlex_quote(remote_dir)} && cat > {shlex_quote(remote_raw)}"
                r = subprocess.run(["ssh", host, cmd], input="\n".join(raw_lines).encode("utf-8"),
                                   capture_output=True, timeout=60)
                if r.returncode != 0:
                    print(f"    WARN: raw.md ssh-cat to topic folder failed for {ck}: {r.stderr.decode(errors='replace')[:200]}")
            else:
                dest = Path(remote_raw).expanduser()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("\n".join(raw_lines), encoding="utf-8")
        except Exception as e:
            print(f"    WARN: raw.md topic copy failed: {e}")

    manifest = {
        "citation_key": ck,
        "doi": art["doi"],
        "title": title,
        "section": art.get("section"),
        "is_oa": art.get("is_oa", False),
        "oa_status": art.get("oa_status", ""),
        "oa_free_url": art.get("oa_free_url", ""),
        "journal": jcfg["name"],
        "journal_slug": journal_key,
        "issue_date": issue_date,
        "article_url": art["article_url"],
        "pdf_url": art["pdf_url"],
        "captured_at": now_iso(),
        "extraction": {
            "tool": "journal-bundler-v0.4.0",
            "abstract_selector": art.get("abstract_selector"),
            "abstract_chars": art.get("abstract_chars", 0),
            "body_selector": art.get("body_selector"),
            "body_chars": art.get("body_chars", 0),
            "figure_count": len(art.get("figures", [])),
        },
        "files": {
            "raw_md_staging": "raw.md",
            "source_pdf": pdf_meta,
            "supplementary": suppl_files,
            "audio": audio_files,
            "video": video_files,
            "topic_promote": (
                {"topic_path": promote[0], "slug": promote[1]} if promote else None
            ),
        },
        "staging_note": "Topic-note folder is staging under journal_toc/<journal>/<date>/articles/<citation_key>/. Move into a topic-only path after a topic-classification pass.",
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return folder


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("journal", choices=list(JOURNALS.keys()))
    ap.add_argument("--issue-date", default=None, help="YYYY-MM-DD; default = today (TST)")
    ap.add_argument("--limit", type=int, default=None, help="extract only first N articles (smoke test)")
    ap.add_argument("--no-pdf", action="store_true", help="skip PDF binary even if config says fetch_pdf")
    ap.add_argument("--dois", default=None, help="comma-separated DOIs to subset (skip others; e.g. 10.7326/ANNALS-25-03691,10.7326/ANNALS-25-03766)")
    ap.add_argument("--force-ingest-all", action="store_true", help="write per-article payloads even when config says wiki_ingest_all=false (use with --dois for designated subset)")
    ap.add_argument("--idempotent", action="store_true", help="exit 0 cleanly if the current TOC's first article DOI is already ingested under any prior date; for scheduled runs that should skip when no new issue has been published")
    ap.add_argument("--cache-dir", default=None, help="cache TOC + per-article extracts under this dir for re-runs")
    ap.add_argument("--per-article-sleep", type=float, default=0.0, help="seconds to sleep between per-article extract calls; needed for JAMA which throttles bursts >~14 sequential XHRs")
    ap.add_argument("--register-mesh", action="store_true", help="after step 3, resolve each article DOI → PubMed PMID → MeSH heading list and UPSERT into medical_knowledge.mesh_descriptor + medical_knowledge.tag (source=MESH). Requires psycopg2 + a reachable PG host and MESH_HELPER_DIR set; skips gracefully if either missing.")
    ap.add_argument("--force-overwrite-vault", action="store_true", help="bypass the vault-mirror regression guard. By default exit_time_archive refuses to overwrite an existing vault mega md when the new capture has <50%% of the previous articles_total (protects against partial-fetch regressions like Lancet 2026-05-16 28→3 article overwrite). Set this only after manual review confirms the new capture is actually correct.")
    ap.add_argument("--toc-url", default=None, help="override the journal's default TOC URL (e.g. for past-issue backfill: --toc-url https://www.nejm.org/toc/nejm/394/18/). Bundler also uses this URL as the session seed when session_seed_url is unset.")
    ap.add_argument("--toc-only", action="store_true", help="Step-0 mode: build the issue mega md straight from the TOC (title/section/type/DOI + RSS abstract excerpt) and SKIP per-article browser body extraction. For RSS journals (Science/Lancet) whose article HTML is Cloudflare-gated and whose bodies aren't needed for the GPT reading-guide step; also avoids the per-article osascript hang. NOTE: even in --toc-only the step-2a Crossref pass fills abstracts by DOI, so this is no longer a content-empty shell.")
    ap.add_argument("--no-crossref", action="store_true", help="skip the step-2a server-side Crossref by-DOI abstract enrichment. By default the bundler fills any empty abstract from api.crossref.org so the harvest is deterministic without a warm browser tab.")
    args = ap.parse_args()

    jcfg = dict(JOURNALS[args.journal])
    if args.toc_url:
        jcfg["toc_url"] = args.toc_url
    issue_date = args.issue_date or today_str()
    fetch_pdf = jcfg["fetch_pdf"] and not args.no_pdf
    fetch_supplementary = jcfg.get("fetch_supplementary", False) and not args.no_pdf
    fetch_audio = jcfg.get("fetch_audio", False)
    ingest_all = jcfg["wiki_ingest_all"] or args.force_ingest_all
    wanted_dois = set(args.dois.split(",")) if args.dois else None

    print(f"== {jcfg['name']} == issue_date={issue_date} fetch_pdf={fetch_pdf} ingest_all={ingest_all}")
    print(f"   media: supplementary={fetch_supplementary} audio={fetch_audio}")
    print(f"   browser={jcfg['browser']}  toc={jcfg['toc_url']}")

    print("[step 1] parsing TOC...")
    toc = parse_toc(jcfg, issue_date=issue_date)
    print(f"  -> {len(toc)} articles found")

    # Idempotency gate (scheduled runs): if the current TOC's first-article
    # citation_key folder already exists under a previous issue_date, NEJM has
    # not yet published a new issue. Exit cleanly so the next scheduled
    # window (e.g. Thu 07:00 retry) can run instead of redoing stale work.
    if args.idempotent and toc:
        first_ck = citation_key(jcfg, toc[0]["doi"])
        weekly_root = WIKI_RAW / "_journal_toc" / args.journal
        if weekly_root.exists():
            # Post-2026-05-14 layout: journal_toc/{journal}/{date}/articles/{ck}
            for date_dir in sorted(weekly_root.glob("[0-9]*-[0-9]*-[0-9]*")):
                if (date_dir / "articles" / first_ck).exists():
                    prior_date = date_dir.name
                    print(f"  -> idempotent skip: first article {toc[0]['doi']} already ingested under {prior_date}; no new issue yet.")
                    return
        # A promote step moves promoted bundles out of _journal_toc into the
        # corpus — also check there so the gate stays correct after promotion.
        _jdir = {"nejm": "NEJM", "aim": "AIM", "bmj": "BMJ", "jama": "JAMA",
                 "lancet": "Lancet", "science": "Science", "nature": "Nature"}
        _ashare = corpus_root() / _jdir.get(args.journal, args.journal.upper())
        if _ashare.exists():
            for _hit in _ashare.rglob(first_ck):
                if _hit.is_dir():
                    print(f"  -> idempotent skip: first article {toc[0]['doi']} already promoted to the corpus ({_hit.parent.name}); no new issue yet.")
                    return

    if wanted_dois:
        toc = [a for a in toc if a["doi"] in wanted_dois]
        missing = wanted_dois - {a["doi"] for a in toc}
        print(f"  -> filtered to {len(toc)} via --dois (missing: {sorted(missing) if missing else 'none'})")
    if args.limit:
        toc = toc[: args.limit]
        print(f"  -> limited to {len(toc)} for smoke test")

    articles = []
    if args.toc_only:
        print("[step 2] --toc-only: skip per-article browser extraction; "
              "mega md built from TOC + RSS abstract excerpt")
        for a in toc:
            merged = dict(a)
            if merged.get("rss_abstract") and not merged.get("abstract"):
                merged["abstract"] = merged["rss_abstract"]
                merged["abstract_source"] = "rss_description"
            articles.append(merged)
        _extract_iter: list = []
    else:
        print("[step 2] extracting per-article HTML + body_md...")
        # For journals
        # whose TOC came from a SERVER-SIDE path (rss/crossref/html — parse_toc
        # opened NO browser tab), the canonical browser per-article extraction
        # silently never worked: no Cloudflare-cleared domain tab existed, so
        # every CF-gated article page (Science/Lancet/...) returned the challenge
        # HTML → extract_article yielded 0 abstract → degraded to the RSS teaser /
        # Crossref. Warm a cleared tab on the domain ONCE before the loop so
        # per-article extract_article actually pulls the (free) open abstract.
        # Graceful: if CF won't clear (cron IP flagged), the loop still falls back
        # to rss_abstract/Crossref below (zero regression).
        if toc and jcfg.get("toc_format") in ("rss", "crossref", "html"):
            warm_url = toc[0].get("article_url") or jcfg.get("toc_url")
            print(f"  [step 2] warming Cloudflare-cleared tab: {warm_url}", flush=True)
            try:
                if not ensure_cleared_tab(jcfg["browser"], warm_url):
                    print("  [step 2] WARN: CF tab not cleared; per-article "
                          "extraction may fall back to RSS/Crossref abstracts", flush=True)
            except Exception as e:
                print(f"  [step 2] warm-tab err {e!r}; continuing", flush=True)
        _extract_iter = list(enumerate(toc, 1))
    for i, a in _extract_iter:
        try:
            if args.per_article_sleep and i > 1:
                time.sleep(args.per_article_sleep)
            print(f"  [{i}/{len(toc)}] {a['doi']} ... ", end="", flush=True)
            ext = extract_article(jcfg, a["article_url"], discover_media=fetch_supplementary or fetch_audio)
            if ext.get("body_md"):
                ext["body_md"] = strip_body_template_junk(ext["body_md"])
                ext["body_chars"] = len(ext["body_md"])
            if ext.get("figures"):
                ext["figures"] = [f for f in ext["figures"]
                                  if not _JUNK_BODY_URL.search(str(f.get("url", "")))]
            merged = {**a, **ext}
            # Lancet/Science RSS-abstract fallback: Cloudflare may block the
            # article HTML even when TOC RSS endpoint passes. RSS feeds carry
            # ~150-700 char abstract excerpts in <description>; use that as
            # abstract when extractor returned empty body. abstract_source flag
            # records where the text came from (extractor / rss).
            if merged.get("rss_abstract") and not merged.get("abstract"):
                merged["abstract"] = merged["rss_abstract"]
                merged["abstract_source"] = "rss_description"
            elif merged.get("abstract"):
                merged["abstract_source"] = "extractor"
            articles.append(merged)
            n_suppl = len((ext.get("media") or {}).get("supplementary", []))
            print(f"abs={ext.get('abstract_chars',0) or len(merged.get('abstract','')) } body={ext.get('body_chars',0)} figs={len(ext.get('figures',[]))} suppl={n_suppl} src={merged.get('abstract_source','-')}")
        except Exception as e:
            print(f"ERR: {e}")
            # Even when per-article extraction fails (e.g. no warm Chrome tab at
            # cron time → ERR:no-matching-tab), preserve the RSS <description>
            # abstract the TOC parser carried (Lancet/Elsevier). Without this the
            # whole issue ships abstract-less — the 2026-06-10 Lancet empty-shell
            # (0/25); step-2a Crossref can't rescue Elsevier (0 deposits). The
            # success branch already applies this fallback; the except branch
            # must too so a fully tab-less run still yields a real reading guide.
            fallback = {**a, "error": str(e)}
            if a.get("rss_abstract"):
                fallback["abstract"] = a["rss_abstract"]
                fallback["abstract_source"] = "rss_description"
                print(f"  -> rss_abstract fallback: {len(a['rss_abstract'])}c")
            articles.append(fallback)

    # Step 2a: Crossref by-DOI abstract enrichment (deterministic,
    # non-agentic). Fills any abstract the browser/RSS path left
    # empty, from api.crossref.org over stdlib urllib — no browser, no warm
    # Cloudflare tab, no agent. Runs in BOTH modes (it is after the toc_only /
    # full assembly merge), so a --toc-only run OR a full run where Cloudflare
    # challenged the article tabs (0 browser abstracts) still yields real
    # abstracts for every DOI-registered article. Fill-the-gaps only: an article
    # that already has a real abstract is left untouched (zero regression).
    # Front-matter pieces (Books/Letters/Working Life) have no abstract anywhere
    # → correctly left blank. Disable with --no-crossref.
    if not args.no_crossref and _crossref is not None and articles and not wanted_dois:
        before = sum(1 for a in articles if (a.get("abstract") or "").strip())
        print("[step 2a] crossref abstract enrichment (by DOI, server-side)...")
        try:
            n_cr, n_try = _crossref.enrich_articles(articles, mailto=CROSSREF_MAILTO)
            after = sum(1 for a in articles if (a.get("abstract") or "").strip())
            print(f"  -> crossref filled {n_cr}/{n_try} missing abstracts; "
                  f"abstracts {before} -> {after}/{len(articles)}")
        except Exception as e:  # noqa: BLE001
            print(f"  WARN crossref enrichment failed (non-fatal): {e}")

    # Step 2a.5: OA identity enrichment. RSS TOCs for Science/Lancet do not
    # expose a reliable OA marker, so hardcoding is_oa=False silently bypassed
    # the mandatory OA full-text lane. Preserve publisher positives and fill
    # only positive Unpaywall verdicts; fresh/unindexed DOIs remain unknown
    # rather than being asserted OA. The helper is bounded against outages.
    if _crossref is not None and articles and not wanted_dois:
        before_oa = sum(1 for a in articles if a.get("is_oa"))
        print("[step 2a.5] Unpaywall OA identity enrichment (by DOI)...")
        try:
            n_oa, n_oa_try, n_oa_unknown = _crossref.enrich_oa_status(
                articles, mailto=CROSSREF_MAILTO)
            after_oa = sum(1 for a in articles if a.get("is_oa"))
            print(f"  -> OA +{n_oa}; {before_oa} -> {after_oa}/{len(articles)} "
                  f"(checked={n_oa_try}, unknown={n_oa_unknown})")
        except Exception as e:  # noqa: BLE001
            print(f"  WARN OA enrichment failed (non-fatal): {e}")

    # Step 2b: podcast/audio-transcript harvesting.
    # When the journal config has fetch_podcast_transcripts=true, scan each
    # article body for publisher audio-player URLs. For each unique audio ID
    # detected, fetch the publisher transcript page via Chrome Beta and write a
    # podcast bundle under $JOURNAL_PODCAST_ROOT/<show>/<year>/<ep>/.
    # Idempotent: existing bundles are skipped (lookup by audio_id substring in
    # manifest.json). Per-article container_section + DOI are passed through so
    # the resulting bundle records the article cross-reference.
    podcast_summary: list[dict] = []
    if jcfg.get("fetch_podcast_transcripts") and _podcast is not None:
        print("[step 2b] harvesting podcast transcripts...")
        seen_audio_ids: set[str] = set()
        for a in articles:
            if a.get("error"):
                continue
            body_md = a.get("body_md") or ""
            if not body_md:
                continue
            links = _podcast.detect_audio_links(body_md, args.journal)
            for link in links:
                aid = link["audio_id"]
                if aid in seen_audio_ids:
                    continue
                seen_audio_ids.add(aid)
                try:
                    result = _podcast.fetch_and_bundle(
                        aid,
                        journal=args.journal,
                        container_section=a.get("section"),
                        related_article_doi=a.get("doi"),
                        related_article_url=a.get("article_url"),
                        fallback_pub_date_iso=issue_date,
                    )
                except Exception as exc:  # noqa: BLE001
                    result = {"status": "error", "audio_id": aid, "error": str(exc)}
                podcast_summary.append(result)
                tag = result.get("status", "?")
                bdir = result.get("bundle_dir", "")
                print(f"    [{tag}] audio_id={aid} -> {bdir}")
        n_written = sum(1 for r in podcast_summary if r.get("status") == "written")
        n_exists = sum(1 for r in podcast_summary if r.get("status") == "exists")
        n_skip   = sum(1 for r in podcast_summary if r.get("status") not in {"written", "exists"})
        print(f"  -> podcasts: {n_written} new, {n_exists} already bundled, {n_skip} skipped/error")

    if wanted_dois:
        # --dois subset is for designated/retro work, not the full issue dump.
        # Skip mega md write so we don't clobber the existing full-issue
        # working file in the inbox (the mega md is THE artifact the operator
        # feeds to GPT for the reading-guide draft).
        print("[step 3] skip mega md (--dois subset run)")
    else:
        print("[step 3] building mega md...")
        # Real issue date from the modal article pub_date (not the run date), unless
        # an explicit --issue-date was given. The TOC
        # bundle must be keyed by the actual issue (e.g. 2026-06-16, Vol 335 No 23),
        # never the day the bundler happened to run. Falls back to run-date if the
        # articles' pub_dates are too few/scattered to be confident.
        if not args.issue_date:
            # Prefer the issue's STATED date from the TOC content (authoritative,
            # works even when articles carry no pub_date e.g. Science/Lancet RSS);
            # fall back to the modal article pub_date; keep the run-date only as a
            # last resort. Fixes the 2026-07-10 wrong-date-folder bug.
            stated = _toc_stated_issue_date(articles, issue_date)
            real_issue_date = stated or _modal_issue_date(articles)
            if real_issue_date and real_issue_date != issue_date:
                src = "TOC-stated date" if stated else "modal pub_date"
                print(f"  -> real issue_date from {src}: {real_issue_date} (was run-date {issue_date})")
                issue_date = real_issue_date
        mega_md = build_mega_md(args.journal, jcfg, issue_date, articles)
        # mega.md is a durable issue artifact, not inbox material. It lives
        # beside the issue reading-guide index.md in the canonical corpus.
        canonical_mega = mega_path(args.journal, issue_date)
        canonical_mega.parent.mkdir(parents=True, exist_ok=True)
        if canonical_mega.exists() and not args.force_overwrite_vault:
            prev_n = _read_articles_total(canonical_mega)
            new_n = _read_articles_total_from_text(mega_md)
            if prev_n and new_n is not None and new_n < prev_n / 2:
                print(f"  REGRESSION GUARD: existing corpus mega.md has articles_total={prev_n}, "
                      f"new capture has {new_n} (<50%); refusing overwrite. "
                      "Re-run with --force-overwrite-vault after manual review.")
                raise SystemExit(3)
        canonical_mega.write_text(mega_md, encoding="utf-8")
        print(f"  -> {canonical_mega} ({len(mega_md)} chars; canonical corpus)")

    if ingest_all:
        print("[step 4] writing per-article payloads (wiki_ingest_all=true)...")
        n_written = 0
        n_pdf = 0
        for art in articles:
            if art.get("error"):
                continue
            pdf_b64 = None
            if fetch_pdf:
                try:
                    pdf = fetch_pdf_base64(jcfg, art["pdf_url"])
                    if pdf.get("is_pdf"):
                        pdf_b64 = pdf["b64"]
                        n_pdf += 1
                    else:
                        print(f"    WARN: pdf {art['doi']} not a PDF: {pdf.get('error') or pdf.get('content_type')}")
                except Exception as e:
                    print(f"    WARN: pdf fetch failed for {art['doi']}: {e}")
            folder = write_per_article_payload(args.journal, jcfg, issue_date, art, pdf_b64)
            n_written += 1
        n_suppl_total = sum(len((a.get("media") or {}).get("supplementary", [])) for a in articles)
        print(f"  -> {n_written} payloads written ({n_pdf} with PDFs, ~{n_suppl_total} supplementary PDFs)")

    if fetch_audio and args.journal == "nejm":
        # Issue-level NEJM audio summary  /do/10.1056/NEJMdo{YYMMDD}/full/
        # YYMMDD derived from issue_date (YYYY-MM-DD).
        yy, mm, dd = issue_date[2:4], issue_date[5:7], issue_date[8:10]
        yymmdd = yy + mm + dd
        print(f"[step 5] discovering issue audio summary NEJMdo{yymmdd} ...")
        audio_dir = WIKI_RAW / "_journal_toc" / args.journal / issue_date / "media"
        audio_dir.mkdir(parents=True, exist_ok=True)
        try:
            disc = discover_issue_audio(jcfg, yymmdd)
            if disc.get("error"):
                print(f"  WARN: audio discovery: {disc.get('error')} ({disc.get('landing_url')})")
            else:
                mp3_url = disc["mp3_url"]
                print(f"  -> mp3_url={mp3_url}")
                bin_r = fetch_binary_base64(jcfg, mp3_url)
                if not bin_r.get("b64"):
                    print(f"  WARN: mp3 fetch empty: {bin_r.get('error')}")
                else:
                    raw_bytes = base64.b64decode(bin_r["b64"])
                    # Issue-level audio summary lands in the same per-issue
                    # subfolder under journal-toc/. macwhisper-watch scans
                    # subfolders recursively, picks it up, transcribes, then
                    # archives the mp3 itself (per its own exit-time rule).
                    audio_dir_inbox = stage1_inbox_dir(args.journal, issue_date)
                    audio_dir_inbox.mkdir(parents=True, exist_ok=True)
                    audio_path = audio_dir_inbox / f"nejm-{issue_date}_audio_summary_nejmdo{yymmdd}.mp3"
                    audio_path.write_bytes(raw_bytes)
                    sha = hashlib.sha256(raw_bytes).hexdigest()
                    print(f"  -> {audio_path} ({len(raw_bytes)} bytes, sha256={sha[:12]}...)")
        except Exception as e:
            print(f"  WARN: audio summary step failed: {e}")

    print("[step 6] mega.md already stored in canonical corpus; no inbox copy")

    if args.register_mesh:
        try:
            _register_mesh_for_issue(articles, journal_key=args.journal, issue_date=issue_date)
        except Exception as e:  # noqa: BLE001
            print(f"[step 7] mesh-register SKIPPED: {e}")
    print("== done ==")


def _register_mesh_for_issue(articles: list, *, journal_key: str, issue_date: str) -> None:
    """Step 7 — auto-register PubMed MeSH headings for each article DOI in the
    issue. Lazy imports so the bundler stays runnable without psycopg2 or the
    MeSH helper modules on PATH.

    The helper modules `mesh_client.py` and `register-mesh-tag.py` must live in
    the directory named by MESH_HELPER_DIR; the feature is off unless that env
    var is set.

    Per-article cost: ~2 NLM calls (doi → pmid, then efetch). Throttled
    by mesh_client at 3 req/sec without API key. ~20 articles ≈ 15 sec.
    """
    import sys as _sys
    mesh_helper_dir = os.environ.get("MESH_HELPER_DIR", "")
    if not mesh_helper_dir:
        raise RuntimeError("set MESH_HELPER_DIR to use --register-mesh")
    helper_dir = Path(mesh_helper_dir).expanduser()
    if str(helper_dir) not in _sys.path:
        _sys.path.insert(0, str(helper_dir))
    import mesh_client  # type: ignore
    import psycopg2  # type: ignore
    register_tool = helper_dir
    if str(register_tool) not in _sys.path:
        _sys.path.insert(0, str(register_tool))
    # reuse helpers from register-mesh-tag.py via importlib (hyphenated module)
    import importlib.util as _il
    spec = _il.spec_from_file_location(
        "register_mesh_tag", str(register_tool / "register-mesh-tag.py"),
    )
    rmt = _il.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(rmt)  # type: ignore

    dsn = os.environ.get("JOURNAL_PG_DSN", f"host={PGHOST} port={PGPORT} user={PGUSER} dbname={PGDATABASE}")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    total_tags = 0
    total_headings = 0
    no_pmid = 0
    try:
        with conn.cursor() as cur:
            for art in articles:
                doi = art.get("doi") or art.get("DOI")
                if not doi:
                    continue
                try:
                    pmid = mesh_client.doi_to_pmid(doi)
                except mesh_client.MeshError:
                    continue
                if not pmid:
                    no_pmid += 1
                    continue
                try:
                    headings = mesh_client.pubmed_mesh(pmid)
                except mesh_client.MeshError:
                    continue
                for h in headings:
                    info = mesh_client.lookup_descriptor(h["ui"])
                    rmt._upsert_descriptor(cur, info, year=int(issue_date[:4]),
                                            picked_by=f"bundler:{journal_key}")
                    inserted = rmt._upsert_tag(
                        cur, info["display"], h["ui"], info.get("category"),
                        f"bundler:{journal_key}",
                        notes=f"from_doi={doi}",
                    )
                    if inserted:
                        total_tags += 1
                total_headings += len(headings)
            conn.commit()
    finally:
        conn.close()
    print(f"[step 7] mesh-register: articles={len(articles)} no_pmid={no_pmid} "
          f"headings={total_headings} new_tags={total_tags}")


def _read_articles_total(path) -> int | None:
    """Return articles_total integer from mega md frontmatter, or None if absent/unparseable."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    for line in head.splitlines():
        if line.startswith("articles_total:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def _read_articles_total_from_text(text: str) -> int | None:
    for line in text[:4096].splitlines():
        if line.startswith("articles_total:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def exit_time_archive(journal_key: str, issue_date: str,
                      wanted_dois: set | None,
                      force_overwrite_vault: bool = False) -> int:
    """Mirror mega md into vault, then move the inbox copy into _archive/.

    Returns number of files archived. Always emits at least one log line
    with the ARCHIVED prefix so the self-audit grep (a future audit_findings
    detector) can confirm bundler ran the archive step."""
    if wanted_dois:
        # --dois subset runs do not write a mega md (see step 3); nothing
        # to archive at this layer. Per-article folders are not staged in
        # inbox so they need no archive.
        print("  -> skip exit-time archive (--dois subset run)")
        return 0
    count = 0
    mega_dir = stage1_inbox_dir(journal_key, issue_date)
    mega_path = mega_dir / f"{journal_key}-{issue_date}.md"
    if not mega_path.exists():
        print(f"  -> exit-time archive: no mega md at {mega_path}")
        return 0
    # Mirror to vault before archive so the GPT-input artifact lives on
    # in the canonical reading library. Path mirrors per-article folder
    # layout: wiki_raw/_journal_toc/{journal}/{date}/{date}.md
    # (Switched 2026-05-14 from articles/{journal}_weekly_issue/{date}.md.)
    vault_dir = WIKI_RAW / "_journal_toc" / journal_key / issue_date
    try:
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault_path = vault_dir / f"{issue_date}.md"
        # Regression guard (added 2026-05-17, post #230031 Lancet 28→3 overwrite):
        # if a prior vault mega md exists for the same issue_date, compare
        # articles_total frontmatter before overwriting. New capture <50%
        # of previous count almost always means partial-fetch / TOC parse
        # degradation, not a corrected count. Refuse overwrite unless
        # --force-overwrite-vault is set after manual review.
        if vault_path.exists() and not force_overwrite_vault:
            prev_n = _read_articles_total(vault_path)
            new_n = _read_articles_total(mega_path)
            if prev_n is not None and new_n is not None and prev_n > 0 and new_n < prev_n / 2:
                print(f"  REGRESSION GUARD: existing vault mega md has articles_total={prev_n}, new capture has {new_n} (<50%); refusing overwrite. Re-run with --force-overwrite-vault after manual review.")
                return 0
        vault_path.write_bytes(mega_path.read_bytes())
        print(f"  -> mirrored mega md → {vault_path}")
    except OSError as e:
        # Vault mirror failed — leave inbox copy in place so the artifact
        # isn't lost; audit-inbox-stale will flag the unarchived file
        # next sweep.
        print(f"  WARN: vault mirror failed, skipping archive: {e}")
        return 0
    # Inbox archive intentionally not done here: the bundler leaves the inbox
    # mega md in place. Archiving is a workflow judgment, not a script
    # side-effect. The mega-md mirror above is the durable copy; the inbox copy
    # stays at the expected <inbox>/journal-toc/{j}/{date}/ path so a manual
    # visit finds it. The operator archives after workflow completion
    # (reading-guide applied, etc.).
    return count


if __name__ == "__main__":
    main()
