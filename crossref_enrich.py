#!/usr/bin/env python3
"""journal-toc Step 2a — deterministic, server-side abstract enrichment by DOI.

The journal-toc pipeline must be deterministic and non-agentic. The canonical
reading-guide harvest (browser per-article `extract_article`) needs a warm,
Cloudflare-cleared browser tab to yield abstracts; when that tab is cold (or the
run used `--toc-only`), the mega-md ships with 0 abstracts — a content-empty
shell.

This module breaks that browser dependency for the ABSTRACT layer: it fetches
each article's abstract from the public Crossref REST API
(`https://api.crossref.org/works/<doi>`) over stdlib urllib — no browser, no
osascript, no auth, no Cloudflare. Coverage is publisher-dependent but high for
the journals that previously had the problem:

    Science  32/40 real abstracts (16/16 Research Articles, 7/7 In Depth,
             4/4 Perspectives; the 8 misses are Books/Letters/Working Life
             that carry no abstract anywhere — correctly left blank).

It is a FILL-THE-GAPS pass: an article that already has a real abstract (from
the subscriber-session browser extractor, or a Lancet RSS <description>) is left
untouched — zero regression for the full-browser-success path. It only fills
articles the upstream path left empty/tiny, so:

  * a --toc-only run, or a full run where Cloudflare challenged every article
    tab (0 browser abstracts), still yields real abstracts for every
    DOI-registered article → the harvest is deterministic & non-agentic.

Used by bundler.py main() between per-article assembly (step 2) and the mega-md
write (step 3). Importable + CLI-testable:

    python3 crossref_enrich.py 10.1126/science.adr6749

Set CROSSREF_MAILTO to your own contact email (Crossref polite pool + Unpaywall).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

CROSSREF_API = "https://api.crossref.org/works/"
UNPAYWALL_API = "https://api.unpaywall.org/v2/"
DEFAULT_MAILTO = os.environ.get("CROSSREF_MAILTO", "journal-bundler@example.com")


def clean_jats(raw: str) -> str:
    """Crossref `abstract` is JATS/XML markup. Convert to flat reading text:
    section <jats:title>s become inline "Label: " prefixes, paragraph/section
    breaks become spaces, every other tag (jats:/mml:/xref/...) is stripped,
    HTML entities are decoded, whitespace collapsed, and a leading bare
    "Abstract" label dropped. Structured abstracts keep their Background/
    Methods/Results/Conclusions cues as readable prose."""
    if not raw:
        return ""
    s = raw
    # <jats:title>Methods</jats:title> -> "Methods: "
    s = re.sub(r"<jats:title>\s*(.*?)\s*</jats:title>",
               lambda m: (m.group(1).rstrip(": ") + ": ") if m.group(1).strip() else "",
               s, flags=re.IGNORECASE | re.DOTALL)
    # paragraph / section closers -> a space so words don't fuse
    s = re.sub(r"</jats:(p|sec|list-item|title)>", " ", s, flags=re.IGNORECASE)
    # strip every remaining tag
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    # drop a leading bare "Abstract" / "Abstract:" / "Summary" label
    s = re.sub(r"^(abstract|summary)\s*[:.\-—]?\s+", "", s, flags=re.IGNORECASE)
    return s.strip()


def fetch_message(doi: str, *, mailto: str = DEFAULT_MAILTO, timeout: int = 12,
                  retries: int = 1) -> dict | None:
    """GET the Crossref work record for `doi`. Returns the `message` object, or
    None on 404 / network error / non-JSON. A mailto in the User-Agent opts into
    Crossref's polite pool (faster, fewer 429s)."""
    if not doi:
        return None
    url = CROSSREF_API + urllib.parse.quote(doi, safe="")
    req = urllib.request.Request(url, headers={
        "User-Agent": f"journal-toc-bundler/1.0 (mailto:{mailto})",
        "Accept": "application/json",
    })
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("message")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # DOI not in Crossref — definitive, no retry
            last_err = e
        except Exception as e:  # noqa: BLE001 — network/JSON/timeout
            last_err = e
        if attempt < retries:
            time.sleep(0.5)
    if last_err is not None:
        raise last_err
    return None


def fetch_abstract(doi: str, *, mailto: str = DEFAULT_MAILTO, timeout: int = 12) -> str:
    """Cleaned plaintext abstract for `doi`, or "" if Crossref has none."""
    msg = fetch_message(doi, mailto=mailto, timeout=timeout)
    if not msg:
        return ""
    return clean_jats(msg.get("abstract", "") or "")


def fetch_oa_verdict(doi: str, *, mailto: str = DEFAULT_MAILTO,
                     timeout: int = 12) -> dict | None:
    """Return Unpaywall's full OA verdict for ``doi``, or ``None``.

    ``None`` means the DOI is not indexed yet (common for a fresh weekly
    issue) or the lookup could not establish a verdict.  Callers must preserve
    any publisher-derived positive marker and must not convert ``None`` into a
    negative OA claim.

    A positive verdict carries ``oa_status`` (gold/hybrid/bronze/green) and the
    best free-copy location so downstream display can distinguish
    publisher-page-free from repository-only green OA (green-OA RCTs were
    otherwise labeled plain "OA" while their publisher links hit the paywall).
    """
    if not doi:
        return None
    url = (UNPAYWALL_API + urllib.parse.quote(doi, safe="") +
           "?email=" + urllib.parse.quote(mailto, safe="@"))
    req = urllib.request.Request(url, headers={
        "User-Agent": f"journal-toc-bundler/1.0 (mailto:{mailto})",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    loc = payload.get("best_oa_location") or {}
    raw_url = loc.get("url_for_landing_page") or loc.get("url") or ""
    # Unpaywall repository deposits occasionally carry trailing junk
    # (e.g. `…Lancet.html>,`); strip stray angle-brackets/commas/whitespace so
    # the `<url>` we emit into the mega is well-formed.
    free_url = raw_url.strip().rstrip(">,; \t")
    if free_url and not free_url.lower().startswith(("http://", "https://")):
        free_url = ""
    return {
        "is_oa": bool(payload.get("is_oa")),
        "oa_status": payload.get("oa_status") or "",
        "host_type": loc.get("host_type") or "",
        "free_url": free_url,
    }


def fetch_oa_status(doi: str, *, mailto: str = DEFAULT_MAILTO,
                    timeout: int = 12) -> bool | None:
    """Boolean-only wrapper around :func:`fetch_oa_verdict` (legacy callers)."""
    verdict = fetch_oa_verdict(doi, mailto=mailto, timeout=timeout)
    return None if verdict is None else verdict["is_oa"]


def enrich_oa_status(articles: list, *, mailto: str = DEFAULT_MAILTO,
                     timeout: int = 12, sleep: float = 0.05,
                     verbose: bool = False) -> tuple[int, int, int]:
    """Fill missing positive OA identities from Unpaywall.

    Returns ``(newly_positive, attempted, unknown)``.  Existing positive
    publisher markers always win.  Network errors are bounded: four
    consecutive failures stop the pass so a transient outage cannot consume a
    scheduled bundler's runtime budget.
    """
    filled = attempted = unknown = consecutive_errors = 0
    for article in articles:
        if article.get("is_oa"):
            continue
        doi = str(article.get("doi") or "").strip()
        if not doi:
            continue
        attempted += 1
        try:
            verdict = fetch_oa_verdict(doi, mailto=mailto, timeout=timeout)
            consecutive_errors = 0
        except Exception as exc:  # noqa: BLE001 - bounded network best effort
            consecutive_errors += 1
            unknown += 1
            if verbose:
                print(f"    [unpaywall error] {doi}: {exc}")
            if consecutive_errors >= 4:
                break
            continue
        if verdict is not None and verdict["is_oa"]:
            article["is_oa"] = True
            article["oa_source"] = "unpaywall"
            article["oa_status"] = verdict["oa_status"]
            article["oa_host_type"] = verdict["host_type"]
            article["oa_free_url"] = verdict["free_url"]
            filled += 1
            if verbose:
                print(f"    [unpaywall OA] {doi} ({verdict['oa_status']})")
        elif verdict is None:
            unknown += 1
        if sleep:
            time.sleep(sleep)
    return filled, attempted, unknown


EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def fetch_abstract_europepmc(doi: str, *, timeout: int = 12) -> str:
    """Cleaned abstract for `doi` from EuropePMC (PubMed + PMC aggregator), or
    "". Second source after Crossref: Crossref deposits no Elsevier abstracts
    and is partial for OUP/ASN, whereas EuropePMC carries the PubMed abstract for
    most indexed articles (and OA full text). No auth, no Cloudflare."""
    if not doi:
        return ""
    q = urllib.parse.quote(f'DOI:"{doi}"')
    url = f"{EUROPEPMC_SEARCH}?query={q}&format=json&pageSize=1&resultType=core"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r).get("resultList", {}).get("result", [])
    except Exception:  # noqa: BLE001
        return ""
    if not res:
        return ""
    return clean_jats(res[0].get("abstractText", "") or "")


def enrich_articles(articles: list, *, mailto: str = DEFAULT_MAILTO,
                    min_chars: int = 60, sleep: float = 0.1,
                    max_consecutive_errors: int = 4, verbose: bool = True) -> tuple:
    """Fill missing abstracts in `articles` (list of dicts with a `doi` key) from
    Crossref, in place. Only articles whose current abstract is shorter than
    `min_chars` are attempted (fill-the-gaps; never overwrites a real abstract).
    On a fill, sets `abstract` + `abstract_source = "crossref"`.

    Bounded for cron safety: if Crossref returns `max_consecutive_errors`
    network errors in a row (API down / rate-limited), stop early rather than
    burn the timeout budget on every remaining DOI.

    Returns (n_filled, n_attempted)."""
    filled = attempted = consec_err = 0
    for a in articles:
        cur = (a.get("abstract") or "").strip()
        if len(cur) >= min_chars:
            continue
        doi = a.get("doi")
        if not doi:
            continue
        attempted += 1
        src = None
        try:
            ab = fetch_abstract(doi, mailto=mailto)
            if ab:
                src = "crossref"
            consec_err = 0
        except Exception as e:  # noqa: BLE001
            ab = ""
            consec_err += 1
            if verbose:
                print(f"    [crossref] {doi} ERR {e}")
            if consec_err >= max_consecutive_errors:
                if verbose:
                    print(f"    [crossref] {consec_err} consecutive errors — "
                          f"Crossref unreachable, stopping enrichment early")
                break
        # Second source: EuropePMC (carries Elsevier/OUP/ASN abstracts Crossref
        # lacks). Only queried when Crossref came back short.
        if (not ab or len(ab) < min_chars):
            epmc = fetch_abstract_europepmc(doi)
            if epmc and len(epmc) >= min_chars:
                ab, src = epmc, "europepmc"
        if sleep:
            time.sleep(sleep)
        if ab and len(ab) >= min_chars:
            a["abstract"] = ab
            a["abstract_source"] = src or "crossref"
            filled += 1
            if verbose:
                print(f"    [{src}] {doi} +{len(ab)}c")
    return filled, attempted


def parse_crossref_recent(issn: str, *, since_date: str | None = None,
                          rows: int = 60, mailto: str = DEFAULT_MAILTO,
                          timeout: int = 25) -> list:
    """TOC source for journals whose publisher RSS / issue pages are bot-blocked
    (NDT / OUP, Kidney360 / ASN). Lists recent journal-articles for `issn` from
    Crossref, newest first, optionally filtered to those published on/after
    `since_date` (a rolling weekly window — OUP/ASN online-first articles lack a
    clean issue boundary). Returns article dicts shaped like the bundler's RSS
    parsers: doi / title / section / type_code / is_oa / rss_abstract /
    article_url / pdf_url. Abstracts come from Crossref where deposited."""
    filt = "type:journal-article"
    if since_date:
        filt += f",from-pub-date:{since_date}"
    url = (f"https://api.crossref.org/journals/{urllib.parse.quote(issn)}/works"
           f"?filter={filt}&sort=published&order=desc&rows={int(rows)}"
           f"&select=DOI,title,abstract,subject,type,license")
    req = urllib.request.Request(url, headers={
        "User-Agent": f"journal-toc-bundler/1.0 (mailto:{mailto})",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        items = json.load(r).get("message", {}).get("items", [])
    out = []
    for it in items:
        doi = (it.get("DOI") or "").strip()
        if not doi:
            continue
        title = (it.get("title") or [""])
        title = re.sub(r"\s+", " ", (title[0] if title else "")).strip()
        subj = it.get("subject") or []
        section = subj[0] if subj else "Article"
        # Gold-OA articles carry a Creative-Commons license in Crossref; paywalled
        # ones carry a publisher TDM license (Elsevier) or none. Hardcoding
        # is_oa=False here meant Kidney360 (journals.json expected_oa_pct: 100)
        # never had a single OA article recognised, so --oa-fetch always reported
        # "0 OA-flagged articles" and its free full text was never ingested.
        licenses = [(l.get("URL") or "") for l in (it.get("license") or [])]
        is_oa = any("creativecommons.org" in u for u in licenses)
        out.append({
            "doi": doi,
            "title": title,
            "section": section,
            "type_code": "",
            "is_oa": is_oa,
            "rss_abstract": clean_jats(it.get("abstract", "") or ""),
            "article_url": f"https://doi.org/{doi}",
            "pdf_url": f"https://doi.org/{doi}",
        })
    return out


def _cli(argv: list) -> int:
    if not argv:
        print("usage: crossref_enrich.py <doi> [<doi> ...]", file=sys.stderr)
        return 2
    for doi in argv:
        ab = fetch_abstract(doi)
        print(f"\n=== {doi} === ({len(ab)} chars)")
        print(ab if ab else "(no abstract in Crossref)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
