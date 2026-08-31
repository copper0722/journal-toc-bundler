---
summary: Public deterministic journal issue extraction and corpus-bundling engine.
owner: copper
---

# journal-toc-bundler

## Purpose

Extract journal issue metadata and authorized full text into reviewable staging bundles,
then support explicit enrichment, media folding, promotion, and registration steps.

## Boundaries

- This repository is a public-safe engine extraction; private orchestration and runtime
  topology stay in their owning private workflow.
- Never commit credentials, cookies, subscriber session state, restricted full text,
  private corpus paths, notification identities, or PostgreSQL connection details.
- Use only open content or content available through the operator's lawful session;
  never bypass publisher authentication or access controls.
- Corpus payload and structured mutable state remain in their configured external
  authorities; Git owns code, contracts, safe configuration, and review history.
- Keep source capture, extraction, enrichment, promotion, and registration as distinct
  provenance stages.
- Read each entrypoint's mutation flags; do not assume every script defaults to dry-run.

## Routing

- `bundler.py`: primary issue acquisition and staging entrypoint.
- `journals.json`: journal identity, endpoints, selectors, and behavior.
- `journal_paths.py`: canonical path construction helpers.
- `crossref_enrich.py` and `nejm_online_first_watch.py`: metadata enrichment and watch.
- `wikify_register.py` and `promote.py`: extraction, registration, and promotion.
- `fold_*.py`, `podcast_bundler.py`, and `nejm-mega-fulltext.py`: media and full-text folding.
- `README.md` and `.env.example`: public interface and semantic runtime configuration.

## Verification

- Parse every Python file before commit and validate `journals.json` as JSON.
- Run `python3 bundler.py --help` after CLI or configuration changes.
- Exercise extraction changes with a bounded non-promoting probe and inspect the emitted
  manifest and payload rather than relying on process exit alone.
- Promotion or PostgreSQL changes require an explicit dry-run where available, verified
  writer authority, and fresh readback from the destination and its consumer.
