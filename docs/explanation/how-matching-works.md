# How matching works

This page explains why semula matches prompts in tiers and why it will not serve a weak guess on its own. For the commands, see [Handle near misses](../tutorial/near-misses.md).

## The problem

Real prompts drift between runs: an extra word, a new timestamp, a path that changed. Exact matching alone would park the same question again and again. Loose matching alone risks returning a recorded answer for a different question, and a wrong answer in a test fails in confusing ways, far from its cause. For a test double, a silent wrong answer is the worst failure.

## The tiers

semula tries cheap, safe checks first and stops at the first that answers:

1. **Exact.** The messages and tools are identical.
2. **Normalised.** The prompts differ only by noise such as timestamps, ids and spacing, plus your own scrub rules.
3. **Alias.** A person approved this prompt as another entry's.
4. **Words.** Nearly all the words are shared.
5. **Meaning.** A paraphrase, found with the optional embeddings.

The first three are exact lookups, so they cannot be wrong in a new way. Only the last two can, which is why they are guarded.

## How the guards work

- A word match is served only at or above a per-project threshold. The default is `1`, so only prompts with the same words are served, and anything near goes to review.
- A near match is also served only with the same shape: the same roles in the same order, the same tools and the same listed parameters. Otherwise a near-identical prompt that offers a different tool could be answered with a call to a tool it lacks.
- A meaning match is never served unless a project opts in with `embedding_serve_threshold`, and every such call is logged with its tier and score.
- A weaker candidate goes to the review queue and the call carries on as a miss. In record mode it parks, so you still get an answer. A person approves the pairing (it becomes an alias) or rejects it for good.

## The trade

You get replay you can trust, at the price of a lower hit rate until near matches have been reviewed. Approving a candidate teaches semula once, and the project's thresholds let you loosen the guard when you have evidence it is safe.

## Why the meaning tier is optional

Embeddings add about 180 MB of libraries and a 65 MB model, and cost tens of milliseconds on a miss. The default install carries neither. Hits and writes never pay for the model. See [Configuration](../reference/configuration.md) for the switches.

## Related

- [Handle near misses](../tutorial/near-misses.md) shows `semula test-match` and the review queue.
- [Concepts](../concepts.md) defines tier and alias.
