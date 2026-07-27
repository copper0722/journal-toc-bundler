#!/usr/bin/env python3
"""journal-toc Step 2d — fold media ASR transcripts into the issue mega-md.

逐字稿是全文的一部份，要進 mega.md。
Every downloaded per-article audio/video is ASR-transcribed (whisper-cpp,
headless — MacWhisper GUI is unreliable on the host) and the transcript text is
folded INTO the issue mega-md under the matching article's section, plus the
issue-level audio summary at the top. Transcripts are part of the article full
text, not a separate sidecar.

Mapping: media filename embeds the citation_key
  <journal>-<date>_<ckey>_<audio|video>_<...>.{mp3,mp4}
  <journal>-<date>_audio_summary_<id>.mp3   (issue-level NEJM This Week)
Article match: ckey == lower(DOI suffix) of the mega "## Article N" section.

Idempotent: re-runs skip sections already carrying a folded transcript.
Silent / figure-only clips (< --min-words) get a note, not junk text.

Usage:
  fold_transcripts.py <issue-dir> [--mega <canonical-mega.md>]
    [--min-words 15] [--model ggml-small] [--fold-only]
  # default: ASR-transcribe any media lacking a sibling <media>.transcript.txt
  # (whisper-cpp, headless), THEN fold all transcripts into mega-md.
  # --fold-only skips transcription (fold pre-existing transcripts only).
"""
import os, sys, re, shutil, argparse, pathlib, subprocess

MARK_ART = "### Media Transcript"
MARK_SUM = "### Issue Audio Summary (transcript)"

WHISPER = os.environ.get("WHISPER_CLI", "whisper-cli")
GGML = pathlib.Path(os.environ.get(
    "WHISPER_MODEL", "~/.cache/whisper-cpp/ggml-small.bin")).expanduser()
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")


def transcribe_missing(d: pathlib.Path) -> int:
    """ASR every *.mp3/*.mp4 lacking a sibling <stem>.transcript.txt.
    whisper-cpp headless (MacWhisper GUI is unreliable on the host over ssh).
    Returns count newly transcribed. Best-effort: missing tool → skip."""
    if not (shutil.which(WHISPER) or pathlib.Path(WHISPER).expanduser().exists()) or not GGML.exists():
        print(f"warn: whisper-cli/{GGML.name} absent — skipping transcription", file=sys.stderr)
        return 0
    n = 0
    for m in sorted([*d.glob("*.mp3"), *d.glob("*.mp4")]):
        out = m.parent / (m.stem + ".transcript.txt")
        if out.exists() and out.stat().st_size > 0:
            continue
        wav = pathlib.Path("/tmp") / (m.stem + ".wav")
        subprocess.run([FFMPEG, "-y", "-i", str(m), "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", str(wav)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if not wav.exists():
            continue
        subprocess.run([WHISPER, "-m", str(GGML), "-f", str(wav), "-l", "en",
                        "-otxt", "-of", str(m.parent / (m.stem + ".transcript")), "-t", "8"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        wav.unlink(missing_ok=True)
        if out.exists():
            n += 1
    return n


def doi_suffix(section: str) -> str | None:
    m = re.search(r"\*\*DOI\*\*:\s*\[([^\]]+)\]", section)
    if not m:
        return None
    return m.group(1).strip().split("/")[-1].lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue_dir")
    ap.add_argument("--mega", help="canonical mega.md to update; media/transcripts stay in issue_dir")
    ap.add_argument("--min-words", type=int, default=15)
    ap.add_argument("--model", default="ggml-small")
    ap.add_argument("--fold-only", action="store_true",
                    help="skip ASR; fold pre-existing *.transcript.txt only")
    args = ap.parse_args()

    d = pathlib.Path(args.issue_dir).expanduser()
    if not args.fold_only:
        nt = transcribe_missing(d)
        if nt:
            print(f"transcribed {nt} media file(s) (whisper-cpp {args.model})")
    if args.mega:
        mega = pathlib.Path(args.mega).expanduser()
        if not mega.is_file():
            print(f"error: canonical mega-md not found: {mega}", file=sys.stderr)
            sys.exit(1)
    else:
        megas = [p for p in d.glob("*.md") if ".transcript" not in p.name]
        if len(megas) != 1:
            print(f"error: expected exactly 1 mega-md in {d}, found {len(megas)}", file=sys.stderr)
            sys.exit(1)
        mega = megas[0]
    prefix = mega.stem + "_"          # e.g. "nejm-2026-06-11_"

    # gather transcripts: ckey/type -> (text, words, is_summary)
    by_ckey: dict[str, list[dict]] = {}
    summary = None
    for tf in sorted(d.glob("*.transcript.txt")):
        rel = tf.name[:-len(".transcript.txt")]
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
        else:
            # When mega.md lives in the canonical corpus but media remains in
            # the inbox, strip the journal/date filename prefix from staging.
            rel = re.sub(r"^[^-]+-\d{4}-\d{2}-\d{2}_", "", rel)
        text = tf.read_text(encoding="utf-8", errors="replace").strip()
        words = len(text.split())
        if rel.startswith("audio_summary"):
            summary = {"text": text, "words": words}
            continue
        parts = rel.split("_")
        ckey = parts[0].lower()
        mtype = parts[1] if len(parts) > 1 and parts[1] in ("audio", "video") else "media"
        by_ckey.setdefault(ckey, []).append({"type": mtype, "text": text, "words": words})

    raw = mega.read_text(encoding="utf-8")
    chunks = raw.split("\n---\n")          # [0]=frontmatter, [1]=intro, [2:]=articles
    if len(chunks) < 2:
        print("error: mega-md has no article separators", file=sys.stderr)
        sys.exit(1)

    folded_arts = 0
    folded_sum = False

    # issue-level audio summary → intro chunk (chunks[1])
    if summary and MARK_SUM not in chunks[1]:
        block = [f"\n{MARK_SUM}", "",
                 f"*NEJM This Week — whisper-cpp {args.model}, {summary['words']} words.*", "",
                 summary["text"], ""]
        chunks[1] = chunks[1].rstrip() + "\n" + "\n".join(block)
        folded_sum = True

    for i in range(2, len(chunks)):
        sec = chunks[i]
        if "## Article" not in sec:
            continue
        if MARK_ART in sec:
            continue
        ck = doi_suffix(sec)
        if not ck or ck not in by_ckey:
            continue
        blocks = []
        for media in by_ckey[ck]:
            blocks.append(f"\n{MARK_ART} ({media['type']})")
            blocks.append("")
            if media["words"] < args.min_words:
                blocks.append(f"*(silent / figure-only {media['type']} — no narration; "
                              f"mp4 retained as figure asset, {media['words']} word(s) ASR)*")
            else:
                blocks.append(f"*ASR via whisper-cpp {args.model}, {media['words']} words. "
                              f"Verbatim — drug/trial names may carry ASR errors.*")
                blocks.append("")
                blocks.append(media["text"])
            blocks.append("")
        chunks[i] = sec.rstrip() + "\n" + "\n".join(blocks)
        folded_arts += 1

    if not folded_arts and not folded_sum:
        print("nothing to fold (already folded or no matching transcripts)")
        return

    mega.with_suffix(".md.bak").write_text(raw, encoding="utf-8")
    mega.write_text("\n---\n".join(chunks), encoding="utf-8")
    print(f"folded: {folded_arts} article transcript(s)"
          f"{' + issue audio summary' if folded_sum else ''} into {mega.name}")
    print(f"backup: {mega.name}.bak")


if __name__ == "__main__":
    main()
