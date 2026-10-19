# ADR-0003: Matching is tiered, and only high-confidence tiers auto-serve

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

Exact matching fails on real prompts. Looser matching risks returning a wrong recorded answer, which breaks tests in confusing ways. Embeddings add weight and a model download.

## Decision

Cascade: (1) exact fingerprint, (2) normalised fingerprint, (3) BM25 over SQLite FTS5. Tiers 1 and 2 auto-serve. BM25 auto-serves only above a per-project high threshold. Anything below goes to a review queue with its score and the call proceeds as a miss. An optional embedding tier (fastembed, bge-small-en-v1.5, behind an extra) never auto-serves by default; a per-project opt-in threshold can allow it, and its use is logged. Every call records which tier answered and the score.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Auto-serve fuzzy matches above 0.90 | Silent wrong answers are the worst failure mode for a test double. |
| Embeddings in the default image | Larger image and cold start for a gain most users have not yet measured. |
| Intent classifier or router | Normalisation and BM25 cover the need until measured otherwise. |

## Consequences

Buys trustworthy replay. Costs some hit rate until a human approves near-misses, and a review workflow to build (SEM-E6). Thresholds need tuning against real recordings.
