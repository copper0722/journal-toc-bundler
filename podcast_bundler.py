#!/usr/bin/env python3
"""podcast_bundler — detect + bundle journal-network podcast transcripts.

Companion module for `bundler.py`. Adds two capabilities:

  1. detect_audio_links(body_md, journal) — scan a per-article body_md for
     publisher-hosted audio-player URLs (e.g. JAMA's
     `jamanetwork.com/learning/audio-player/<id>` which redirects to AMA EdHub
     `edhub.ama-assn.org/jn-learning/audio-player/<id>`).
  2. fetch_and_bundle(audio_id, ...) — drive the publisher-side logged-in Chrome
     Beta via osascript to the EdHub transcript page, extract title + dates +
     verbatim transcript, infer the parent show, and write a podcast bundle to
     `$JOURNAL_PODCAST_ROOT/<show_slug>/<year>/<episode_slug>/`
     (raw.md + manifest.json) following the podcast subtype bundle contract.

Per-journal URL patterns + show-inference rules live in this module so the same
detector can be invoked from manual scripts (one-off audio_id) and from
`bundler.py` during JAMA / NEJM / etc. issue bundling.

CLI:
    python3 podcast_bundler.py fetch <audio_id_or_url> [--journal jama]
        [--related-article-doi 10.1001/...] [--issue-date YYYY-MM-DD]

Idempotent: if the target bundle directory already has raw.md, returns the
existing path without refetching.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants

CORPUS_ROOT = Path(os.environ.get(
    "JOURNAL_PODCAST_ROOT", str(Path.home() / "journal-corpus" / "podcast"))).expanduser()

# Per-journal audio-link URL patterns.
# JAMA exposes each audio under TWO URL forms in the article body:
#   (1) numeric AMA EdHub player ID:   /audio-player/19059356
#   (2) DOI form (audio's own DOI):    /audio-player/10.1001/jama.2026.5869
# Both resolve to the same transcript page. The numeric form is preferred for
# fetching (stable, no slash to escape). We capture both and the orchestrator
# dedupes against existing bundles. Patterns are anchored on a non-alnum
# character class so the DOI form captures the full DOI without losing the
# trailing segment after the first dot.
AUDIO_URL_PATTERNS: dict[str, list[str]] = {
    "jama": [
        # Numeric player ID (preferred): \d+ stops at non-digit boundary
        r"https?://(?:jamanetwork\.com/learning|edhub\.ama-assn\.org/jn-learning)/audio-player/(\d+)(?![./\d])",
        # DOI form: captures 10.<reg>/<rest> until whitespace/quote/paren/anchor
        r"https?://(?:jamanetwork\.com/learning|edhub\.ama-assn\.org/jn-learning)/audio-player/(10\.\d{4,}/[^\s)\"'#?<>]+)",
    ],
    # NEJM audio lives at /doi/full/<doi>/audio/* — captured via the article DOI
    # not a separate ID; supplementary pipeline already handles those. Leave
    # empty until a concrete pattern is needed.
    "nejm": [],
}

PUBLISHER_TRANSCRIPT_URL = {
    "jama": "https://edhub.ama-assn.org/jn-learning/audio-player/{audio_id}",
    # NEJM-style would be different; add when wiring.
}

# Show inference: regex against (transcript intro + page meta blob + h1 + title).
# First match wins. Ordered most-specific → fallback.
# Each tuple: (compiled_regex, show_slug, full_show_name).
_SHOW_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"JAMA Editor.{0,3}s Summary", re.IGNORECASE),
     "jama-editors-summary", "JAMA Editor's Summary"),
    (re.compile(r"\bthis is JAMA Clinical Reviews\b", re.IGNORECASE),
     "jama-clinical-reviews", "JAMA Clinical Reviews"),
    (re.compile(r"\bJAMA Clinical Reviews\b", re.IGNORECASE),
     "jama-clinical-reviews", "JAMA Clinical Reviews"),
    (re.compile(r"\bJAMA Medical News\b", re.IGNORECASE),
     "jama-medical-news", "JAMA Medical News"),
    (re.compile(r"\bJAMA Author Interviews?\b", re.IGNORECASE),
     "jama-author-interviews", "JAMA Author Interviews"),
    (re.compile(r"\bConversations with Dr\.? Bauchner\b", re.IGNORECASE),
     "jama-conversations", "Conversations with Dr Bauchner"),
    (re.compile(r"\bJAMA Network\b.*\bpodcast\b", re.IGNORECASE | re.DOTALL),
     "jama-network-podcast", "JAMA Network Podcast"),
]

# Heuristic: container article's "Section" field can map directly when the
# transcript text is too thin to infer. Per-journal lookup.
SECTION_SHOW_FALLBACK: dict[str, dict[str, tuple[str, str]]] = {
    "jama": {
        "this week in jama":     ("jama-editors-summary",  "JAMA Editor's Summary"),
        "medical news":          ("jama-medical-news",     "JAMA Medical News"),
        "medical news in brief": ("jama-medical-news",     "JAMA Medical News"),
        "trials":                ("jama-clinical-reviews", "JAMA Clinical Reviews"),
    },
}

DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2}),?\s+"
    r"(?P<year>20\d\d)\b"
)
_MONTHS = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August",
     "September","October","November","December"], start=1)}
DURATION_RE = re.compile(r"(\d{1,2})\s*min\s*(\d{1,2})\s*sec", re.IGNORECASE)
DURATION_PAREN_RE = re.compile(r"\((\d{1,2}):(\d{2})\)")  # "(12:02)" form
SLUG_KILL_RE = re.compile(r"[^a-z0-9]+")

# --------------------------------------------------------------------------- #
# Detection from body_md


def detect_audio_links(body_md: str, journal: str) -> list[dict]:
    """Return deduped list of {audio_id, raw_url, source_journal} for audio
    URLs found in the article body markdown.

    JAMA exposes each audio in two URL forms (numeric player ID + DOI form).
    When both are present in the body, the DOI form is suppressed — it's a
    cross-listing/citation, the numeric form is the player widget = canonical
    placement. If only DOI form is present, it is returned and the fetch path
    will use the DOI URL directly."""
    patterns = AUDIO_URL_PATTERNS.get(journal, [])
    numeric: dict[str, dict] = {}
    doi_form: dict[str, dict] = {}
    for pat in patterns:
        for m in re.finditer(pat, body_md, flags=re.IGNORECASE):
            aid = m.group(1)
            entry = {"audio_id": aid, "raw_url": m.group(0),
                     "source_journal": journal,
                     "id_kind": "numeric" if aid.isdigit() else "doi"}
            target = numeric if entry["id_kind"] == "numeric" else doi_form
            target.setdefault(aid, entry)
    # When the body has both numeric and DOI forms, assume DOI forms are
    # cross-listings to audios already seen as numeric. This avoids duplicate
    # bundle creation on JAMA-style "listing + widget" pages.
    if numeric and doi_form:
        return list(numeric.values())
    return list(numeric.values()) + list(doi_form.values())


# --------------------------------------------------------------------------- #
# Transcript fetch via Chrome Beta + osascript

_APPLESCRIPT_FETCH = r"""
on run argv
  set u to item 1 of argv
  set out to item 2 of argv
  tell application "Google Chrome Beta"
    activate
    if (count of windows) is 0 then
      make new window
    end if
    set targetTab to make new tab at end of tabs of front window with properties {URL:u}
    set tries to 0
    repeat
      try
        set s to execute targetTab javascript "document.readyState"
        if s is "complete" then exit repeat
      end try
      delay 0.5
      set tries to tries + 1
      if tries > 60 then exit repeat
    end repeat
    delay 3
    set js to "(function(){
      var o = { url: location.href, title: document.title || '',
                h1: ((document.querySelector('h1')||{}).innerText)||'',
                transcript:'', dates:[], meta_blob:''};
      var t = document.getElementById('edhub-transcript');
      if (t) { o.transcript = t.innerText; }
      else {
        var sel = ['[data-tab-content=transcript]','section.transcript','div.transcript','div[id*=transcript]','div[class*=transcript]'];
        for (var i=0;i<sel.length;i++){ var el=document.querySelector(sel[i]); if(el){o.transcript=el.innerText; break;} }
      }
      var bodyText = document.body ? document.body.innerText : '';
      var dateRe = /(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},?\\s+20\\d\\d/g;
      var dset = {}; var m;
      while ((m=dateRe.exec(bodyText))!==null){ dset[m[0]] = true; }
      o.dates = Object.keys(dset);
      var sels = ['header','[class*=metadata]','[class*=Header]','time','[class*=info]','[class*=Detail]','[class*=detail]'];
      var blob = [];
      sels.forEach(function(s){
        document.querySelectorAll(s).forEach(function(e){
          var t = e.innerText;
          if (t && t.length>3 && t.length<400 && blob.length<20) blob.push(t.replace(/\\s+/g,' ').trim());
        });
      });
      o.meta_blob = blob.join(' | ');
      o.transcript_len = o.transcript.length;
      return JSON.stringify(o);
    })()"
    set raw to execute targetTab javascript js
    set fh to open for access POSIX file out with write permission
    set eof of fh to 0
    write raw to fh as «class utf8»
    close access fh
    close targetTab
  end tell
end run
"""


def fetch_transcript_page(url: str) -> dict:
    """Open `url` in Chrome Beta via AppleScript, extract transcript + meta."""
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as af:
        af.write(_APPLESCRIPT_FETCH)
        script_path = af.name
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as outf:
        out_path = outf.name
    try:
        subprocess.run(
            ["osascript", script_path, url, out_path],
            check=True, capture_output=True, text=True, timeout=180,
        )
        return json.loads(Path(out_path).read_text(encoding="utf-8"))
    finally:
        for p in (script_path, out_path):
            try: os.unlink(p)
            except OSError: pass


# --------------------------------------------------------------------------- #
# Show + slug inference


def derive_show(transcript: str, page_title: str, page_meta: str,
                container_section: str | None = None,
                journal: str = "jama") -> tuple[str, str]:
    """Return (show_slug, show_full_name). Tries: transcript intro/meta regex →
    container-article section fallback → journal default."""
    blob = " ".join(s[:2000] for s in (transcript, page_meta, page_title) if s)
    for pat, slug, name in _SHOW_RULES:
        if pat.search(blob):
            return slug, name
    if container_section:
        sec = container_section.strip().lower()
        m = SECTION_SHOW_FALLBACK.get(journal, {}).get(sec)
        if m: return m
    # Final fallback per journal
    if journal == "jama":
        return "jama-network-podcast", "JAMA Network Podcast"
    return f"{journal}-podcast", f"{journal.upper()} Podcast"


def derive_publication_date(meta_dates: list[str], page_meta: str,
                            fallback_iso: str | None = None) -> str | None:
    """Pick the most likely publication date as YYYY-MM-DD.
    Priority: 'Published Online: <date>' in page_meta → first date in meta_dates
    → fallback_iso."""
    m = re.search(r"Published\s+(?:Online|on)\s*:\s*"
                  r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d\d)",
                  page_meta, flags=re.IGNORECASE)
    if m:
        return _date_iso(m.group(1), m.group(2), m.group(3))
    for d in meta_dates:
        mm = DATE_RE.search(d)
        if mm:
            return _date_iso(mm.group("month"), mm.group("day"), mm.group("year"))
    return fallback_iso


def _date_iso(month: str, day: str, year: str) -> str:
    mi = _MONTHS[month.title()]
    return f"{int(year):04d}-{mi:02d}-{int(day):02d}"


def derive_duration_sec(page_meta: str) -> int | None:
    m = DURATION_RE.search(page_meta)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = DURATION_PAREN_RE.search(page_meta)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def derive_episode_slug(pub_date_iso: str, title: str, show_slug: str) -> str:
    """`<YYYY-MM-DD>_<title-keyword>` per the podcast bundle naming convention."""
    # Strip the publisher's section prefix (e.g. "GENETICS AND GENOMICS | Why...")
    # and any pipe-separated tail.
    t = title.split("|", 1)[-1].strip() if "|" in title else title
    t = re.sub(r"\b(JN Learning|AMA Ed Hub|JAMA Network)\b", "", t, flags=re.IGNORECASE)
    t = t.lower().strip()
    # Short show-specific override: weekly editor's summaries are best slug'd as
    # "audio-highlights" + date (their titles are a content list, not a name).
    if show_slug == "jama-editors-summary":
        return f"{pub_date_iso}_audio-highlights"
    # Generic: take first 5 meaningful words
    words = [w for w in SLUG_KILL_RE.split(t) if w and w not in
             {"the","a","an","of","for","with","on","in","and","or","to","by"}]
    keyword = "-".join(words[:5]) or "episode"
    return f"{pub_date_iso}_{keyword}"


# --------------------------------------------------------------------------- #
# Bundle write


def write_bundle(bundle_dir: Path, *,
                 audio_id: str, journal: str, show_slug: str, show_name: str,
                 episode_slug: str, pub_date_iso: str | None, year: int,
                 transcript: str, title: str, page_meta: str,
                 duration_sec: int | None,
                 canonical_url: str,
                 related_article_doi: str | None = None,
                 related_article_url: str | None = None,
                 container_section: str | None = None) -> dict:
    """Write raw.md + manifest.json. Returns metadata dict.

    Idempotent: if raw.md already exists, returns existing without rewriting."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    raw_path = bundle_dir / "raw.md"
    manifest_path = bundle_dir / "manifest.json"

    citation_key = f"JAMA_{show_slug.replace('jama-','').replace('-','_')}_{audio_id}" \
        if journal == "jama" else f"{journal.upper()}_{show_slug.replace('-','_')}_{audio_id}"
    uid = f"url:edhub.ama-assn.org/jn-learning/audio-player/{audio_id}" \
        if journal == "jama" else f"url:{canonical_url.replace('https://','').replace('http://','')}"

    if raw_path.exists():
        existing = {"bundle_dir": str(bundle_dir), "status": "exists", "audio_id": audio_id}
        return existing

    # Hosts: best-effort first speaker turn from transcript
    hosts = _guess_hosts(transcript)

    fm_lines = [
        "---",
        f"citationKey: {citation_key}",
        f'uid: "{uid}"',
        f'payload: "{CORPUS_ROOT}/{show_slug}/{year}/{episode_slug}"',
        "type: raw",
        "source_type: podcast",
        f'title: "{_yaml_quote(title)}"',
        f'show: "{_yaml_quote(show_name)}"',
        f"publication_date: {pub_date_iso or ''}",
        f"year: {year}",
        f'hosts: "{", ".join(hosts) if hosts else ""}"',
        f'publisher: "{_publisher_for(journal)}"',
        f'canonical_url: "{canonical_url}"',
        f'section: "{container_section or ""}"',
        f"duration_sec: {duration_sec if duration_sec is not None else ''}",
    ]
    if related_article_doi:
        fm_lines.append(f'related_article_doi: "{related_article_doi}"')
    if related_article_url:
        fm_lines.append(f'related_article_url: "{related_article_url}"')
    fm_lines += [
        "source_form: audio_transcript_web",
        "lang: en",
        "source_lang: en",
        f"generated: {time.strftime('%Y-%m-%d')}",
        "agent: edhub-transcript-fetch",
        "tags: []",
        f'summary: "Auto-bundled by journal-toc bundler — {show_name} episode (audio_id={audio_id})."',
        "---",
    ]

    body_lines = [
        "",
        f"# {title}",
        "",
        f"**Source**: {canonical_url}",
        f"**Fetched**: {time.strftime('%Y-%m-%d')} (Chrome Beta + AppleScript, transcript extracted from `#edhub-transcript`).",
    ]
    if related_article_doi:
        body_lines += [
            "",
            f"**Related {journal.upper()} article**: "
            f"[{related_article_doi}](https://doi.org/{related_article_doi})",
        ]
    body_lines += [
        "",
        "---",
        "",
        "## Verbatim Transcript",
        "",
        transcript.strip(),
        "",
    ]
    raw_path.write_text("\n".join(fm_lines) + "\n" + "\n".join(body_lines), encoding="utf-8")

    manifest = {
        "type": "podcast_episode",
        "source_type": "podcast",
        "uid": uid,
        "citation_key": citation_key,
        "show": show_name,
        "show_slug": show_slug,
        "episode_title": title,
        "publication_date": pub_date_iso,
        "year": year,
        "hosts": hosts,
        "publisher": _publisher_for(journal),
        "canonical_url": canonical_url,
        "section": container_section,
        "duration_sec": duration_sec,
        "source_form": "audio_transcript_web",
        "source_files": [],
        "lang": "en",
        "source_lang": "en",
        "primary_topic": None,
        "tags": [],
        "topics_in_episode": [],
        "ingested_at": time.strftime("%Y-%m-%d"),
        "ingested_by": "journal-toc-bundler/podcast_bundler.py",
        "wiki_slug": None,
    }
    if related_article_doi:
        manifest["related_article_doi"] = related_article_doi
    if related_article_url:
        manifest["related_article_url"] = related_article_url

    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    return {"bundle_dir": str(bundle_dir), "status": "written", "audio_id": audio_id}


def _publisher_for(journal: str) -> str:
    return {
        "jama": "American Medical Association / JAMA Network",
        "nejm": "Massachusetts Medical Society / NEJM Group",
    }.get(journal, journal.upper())


def _yaml_quote(s: str) -> str:
    return s.replace('"', '\\"')


def _guess_hosts(transcript: str) -> list[str]:
    """Pull speaker labels from first ~3000 chars. Returns up to 2 unique names."""
    speakers: list[str] = []
    for m in re.finditer(r"^([A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'\-]+)?)\s*:",
                         transcript[:3000], flags=re.MULTILINE):
        name = m.group(1)
        if name in {"Announcer", "Intro", "Transcript"}: continue
        if name not in speakers:
            speakers.append(name)
        if len(speakers) >= 2: break
    return speakers


# --------------------------------------------------------------------------- #
# Public entry point


def fetch_and_bundle(audio_id: str, *,
                     journal: str = "jama",
                     container_section: str | None = None,
                     related_article_doi: str | None = None,
                     related_article_url: str | None = None,
                     fallback_pub_date_iso: str | None = None) -> dict:
    """Fetch transcript page for `audio_id`, derive metadata, write bundle.

    Idempotency: caller may pre-check; this function also checks for existing
    raw.md before fetching to save the AppleScript round-trip.

    Returns: {bundle_dir, status: 'exists'|'written'|'no_transcript', ...}.
    """
    transcript_url_tpl = PUBLISHER_TRANSCRIPT_URL.get(journal)
    if not transcript_url_tpl:
        return {"status": "unsupported_journal", "journal": journal}
    canonical_url = transcript_url_tpl.format(audio_id=audio_id)

    # Light dedup: scan existing podcast bundles for the audio_id stamp in
    # manifest.json / raw.md frontmatter. If found, skip fetch.
    existing = _find_existing_bundle(audio_id)
    if existing:
        return {"status": "exists", "bundle_dir": str(existing),
                "audio_id": audio_id}

    page = fetch_transcript_page(canonical_url)
    transcript = page.get("transcript", "")
    if not transcript or len(transcript) < 200:
        return {"status": "no_transcript", "audio_id": audio_id,
                "canonical_url": canonical_url,
                "page_title": page.get("title")}

    title = page.get("h1") or page.get("title") or f"audio-{audio_id}"
    page_meta = page.get("meta_blob", "")
    pub_date_iso = derive_publication_date(page.get("dates", []), page_meta,
                                            fallback_pub_date_iso)
    if not pub_date_iso:
        # Last-resort: today, but flag in manifest by leaving pub_date None.
        # We still need YEAR for folder; default to current year.
        year = int(time.strftime("%Y"))
        pub_date_iso_for_slug = time.strftime("%Y-%m-%d")
    else:
        year = int(pub_date_iso[:4])
        pub_date_iso_for_slug = pub_date_iso

    duration_sec = derive_duration_sec(page_meta)
    show_slug, show_name = derive_show(transcript, page.get("title", ""),
                                        page_meta, container_section, journal)
    episode_slug = derive_episode_slug(pub_date_iso_for_slug, title, show_slug)
    bundle_dir = CORPUS_ROOT / show_slug / str(year) / episode_slug

    return write_bundle(
        bundle_dir,
        audio_id=audio_id, journal=journal,
        show_slug=show_slug, show_name=show_name,
        episode_slug=episode_slug, pub_date_iso=pub_date_iso, year=year,
        transcript=transcript, title=title, page_meta=page_meta,
        duration_sec=duration_sec,
        canonical_url=canonical_url,
        related_article_doi=related_article_doi,
        related_article_url=related_article_url,
        container_section=container_section,
    )


def _find_existing_bundle(audio_id: str) -> Path | None:
    """Grep manifest.json files under CORPUS_ROOT for the audio_id. Cheap."""
    if not CORPUS_ROOT.exists():
        return None
    needle_a = f'"audio-player/{audio_id}"'  # canonical_url substring
    needle_b = f"_{audio_id}\""  # citation_key tail
    needle_c = f"audio-player/{audio_id}"
    for manifest in CORPUS_ROOT.rglob("manifest.json"):
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle_a in text or needle_b in text or needle_c in text:
            return manifest.parent
    return None


# --------------------------------------------------------------------------- #
# CLI


def _cli():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fetch one transcript by audio_id or URL")
    f.add_argument("audio", help="audio_id (digits) OR EdHub/jamanetwork audio URL")
    f.add_argument("--journal", default="jama", choices=list(PUBLISHER_TRANSCRIPT_URL.keys()))
    f.add_argument("--related-article-doi", default=None)
    f.add_argument("--related-article-url", default=None)
    f.add_argument("--container-section", default=None,
                   help="The parent article's TOC section (e.g. 'Medical News')")
    f.add_argument("--fallback-date", default=None,
                   help="YYYY-MM-DD if page metadata lacks a date")
    args = ap.parse_args()

    if args.cmd == "fetch":
        # Accept raw ID or full URL
        m = re.search(r"audio-player/(\d+)", args.audio)
        audio_id = m.group(1) if m else args.audio
        result = fetch_and_bundle(
            audio_id, journal=args.journal,
            container_section=args.container_section,
            related_article_doi=args.related_article_doi,
            related_article_url=args.related_article_url,
            fallback_pub_date_iso=args.fallback_date,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
