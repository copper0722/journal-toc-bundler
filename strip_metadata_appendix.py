#!/usr/bin/env python3
"""Strip the machine-only `## Article-level structured metadata` fenced-JSON
appendix from published journal-summary index.md files before they reach the
public site.

The machine-only JSON appendix is LLM-RAG-only and must not appear on a public
reader page. The per-article structured data already lives in the issue mega-md.

Idempotent. Backs up <file>.md.bak-metastrip on first strip.
Usage:
  strip_metadata_appendix.py <index.md> [<index.md> ...]
  strip_metadata_appendix.py --all          # sweep every corpus journal-summary
"""
import sys, re, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.resolve()))
import journal_paths  # noqa: E402  same-dir import; provides corpus_root()

PAT = re.compile(r"\n#{2,6}[ \t]+Article-level structured metadata\b.*?(?=\n#{2,6}[ \t]|\Z)",
                 re.S | re.I)


def strip_one(p: pathlib.Path) -> bool:
    t = p.read_text(encoding="utf-8")
    new = PAT.sub("\n", t)
    if new == t:
        return False
    p.with_suffix(".md.bak-metastrip").write_text(t, encoding="utf-8")
    p.write_text(new.rstrip() + "\n", encoding="utf-8")
    return True


def main():
    args = sys.argv[1:]
    if args == ["--all"]:
        root = journal_paths.corpus_root()
        targets = [p for p in root.rglob("index.md")
                   if "Article-level structured metadata" in
                   p.read_text(encoding="utf-8", errors="replace")]
    else:
        targets = [pathlib.Path(a).expanduser() for a in args]
    if not targets:
        print("no targets (usage: <index.md>... | --all)", file=sys.stderr)
        sys.exit(1)
    n = 0
    for p in targets:
        if strip_one(p):
            print("stripped", p)
            n += 1
        else:
            print("no-appendix", p)
    print(f"done: {n} stripped")


if __name__ == "__main__":
    main()
