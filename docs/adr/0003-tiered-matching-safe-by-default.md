# ADR-0003: Matching is tiered, and only high-confidence tiers auto-serve

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

Exact matching fails on real prompts. Looser matching risks returning a wrong recorded answer, which breaks tests in confusing ways. Embeddings add weight and a model download.

## Decision

Cascade: (1) exact fingerprint, (2) normalised fingerprint, (3) BM25 over SQLite FTS5. Tiers 1 and 2 auto-serve. BM25 auto-serves only above a per-project high threshold. Anything below goes to a review queue with its score and the call proceeds as a miss. An optional embedding tier (fastembed, bge-small-en-v1.5, behind an extra) never auto-serves by default; a per-project opt-in threshold can allow it, and its use is logged. Every call records which tier answered and the score.

## Amendment: how the lexical tier scores and what it guards (SEM-E6, 2026-10-20)

- **Order.** exact, normalised, alias (an approved candidate), then lexical. The first three are exact lookups; only the lexical tier can be wrong in a new way.
- **Retrieval and score are separate.** The index (`semula/lexical.py`) proposes up to 10 entries: SQLite FTS5 ranked by BM25. Raw BM25 depends on corpus size and is near zero on a small project, so it cannot carry a threshold. The service therefore scores each proposal itself with word-overlap (Sorensen-Dice over the scrubbed words, 0 to 1) and applies the thresholds to that. The score logged in `call_log` and shown in the review queue is this one.
- **Thresholds (per project).** `bm25_serve_threshold` default 1 (only prompts with the same words auto-serve, so in practice everything near goes to review); `bm25_review_threshold` default 0.6. Lowering serve to 0.97 lets a 35-word prompt tolerate one extra word. The review floor is untuned: see Consequences. The serve default was 0.97 for a day and was raised to 1 as the lower-risk choice until tuned on real recordings.
- **Auto-serve also needs the same shape**: the same roles in the same order (after `tail_n`), the same tools, and equal values for any `include_params`. Otherwise a near-identical prompt that offers a different tool could be served an answer that calls a tool it lacks. Such a match goes to review instead.
- **Review queue.** A candidate (request, entry, score) is stored per missed request, at most 3, and never twice for the same pairing. The call stays a miss. A rejection is kept, so that pairing is never queued or served again. Approval writes an alias from the request's normalised fingerprint to the entry, and closes the other open candidates for that request. An answered request closes its own.
- **Other backends.** `ScanIndex` ranks by word overlap on every search (first 5000 entries), so the tier works on Postgres and without FTS5. Hosted Postgres should get a `tsvector` implementation behind the same interface.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Auto-serve fuzzy matches above 0.90 | Silent wrong answers are the worst failure mode for a test double. |
| Embeddings in the default image | Larger image and cold start for a gain most users have not yet measured. |
| Intent classifier or router | Normalisation and BM25 cover the need until measured otherwise. |

## Consequences

Buys trustworthy replay. Costs some hit rate until a human approves near-misses, and a review workflow to build (SEM-E6). Thresholds need tuning against real recordings.
