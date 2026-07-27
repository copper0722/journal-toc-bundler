"""Canonical paths for journal-TOC issue artifacts."""
from __future__ import annotations

import os
from pathlib import Path

JOURNAL_DIRS = {
    "aim": "AIM", "nejm": "NEJM", "jama": "JAMA", "jamaoto": "JAMA-Oto",
    "nature": "Nature", "science": "Science", "lancet": "Lancet", "bmj": "BMJ",
    "jasn": "JASN", "cjasn": "CJASN", "drugs": "Drugs", "jfda": "JFDA",
    "ki": "KidneyInt", "ajkd": "AJKD", "ndt": "NDT", "kidney360": "Kidney360",
}

def corpus_root() -> Path:
    return Path(os.environ.get(
        "JOURNAL_CORPUS_ROOT", str(Path.home() / "journal-corpus" / "journal")
    )).expanduser()

def issue_dir(journal: str, issue_date: str) -> Path:
    return corpus_root() / JOURNAL_DIRS.get(journal.lower(), journal.upper()) / issue_date[:4] / issue_date

def mega_path(journal: str, issue_date: str) -> Path:
    return issue_dir(journal, issue_date) / "mega.md"
