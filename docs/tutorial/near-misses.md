# Near misses and the review queue

An exact match is easy. Real prompts drift: an extra word, a new timestamp, a path that changed. semula checks several *tiers*, cheapest first, and stops at the first that answers.

| Tier | Matches when | Served? |
|---|---|---|
| `exact` | the messages and tools are identical | yes |
| `normalised` | they differ only by noise: timestamps, ids, paths, spacing, and your own scrub rules | yes |
| `alias` | a human approved this prompt as another entry's | yes |
| `words` | nearly all words are shared, same roles, tools and params | only above a threshold you set |
| `meaning` | a paraphrase, with the optional embeddings | only if you opt in |

The rule that matters: **a weak guess is never served by default.** A prompt that only nearly matches is a normal miss, and the pairing waits for a human.

## Ask semula why

`test-match` explains a match or a miss without side effects. First a prompt that differs only by trailing spaces:

```bash
docker exec semula semula test-match --project support-bot --prompt "Classify: my screen is cracked  "
```

```text
match: normalised (1.00) -> entry 01M5HH1FQC65G543SK7JAABCJV (would serve)
tiers: exact no, normalised match 1.00, alias skipped, words skipped, meaning off
```

Now one with an extra word:

```bash
docker exec semula semula test-match --project support-bot --prompt "Classify: my screen is badly cracked"
```

```text
no match (would park); nearest 01M5HH1FQC65G543SK7JAABCJV score 0.909; 1 weak candidate(s) would go to review
tiers: exact no, normalised no, alias no, words candidate 0.91, meaning off
```

`candidate` means: close enough to ask a human, not close enough to serve.

The dashboard's **Playground** does the same with a box to type in: it shows each tier's verdict, the answer it would serve, or the nearest prompt on a miss.

## Decide in the review queue

Send that prompt for real, with the project in record mode. The call parks as usual, and the pairing appears in the review queue:

```bash
docker exec semula semula review list
```

```text
01M5HH2EBM7AV0TKB5H9MCBNDC	open	0.91	support-bot	Classify: my screen is badly cracked  ~  Classify: my screen is cracked
```

`review show <id>` puts the two requests side by side. If the existing answer is right for the new prompt, approve it:

```bash
docker exec semula semula review approve 01M5HH2EBM7AV0TKB5H9MCBNDC
```

```text
approved candidate 01M5HH2EBM7AV0TKB5H9MCBNDC
```

The dashboard has a **Review** screen for the same job: pick a near miss, read the two prompts side by side with the changed words marked, look at the recorded answer, then approve or reject.

From now on that prompt is served by the `alias` tier, edits to the entry included. If the answer is wrong for it, `review reject <id>` and that pairing never comes back.

!!! note "The parked call still needs an answer"
    Approving only teaches semula about the *next* call. The one already waiting is still parked, and you still answer it.

## Tune matching per project

When near-identical prompts keep missing, change the project's settings rather than loosening everything:

| Setting | What it does |
|---|---|
| `scrub_patterns` | regexes to ignore, for example `ticket-\d+` |
| `tail_n` | match only the last N messages |
| `include_params` | request parameters that count, for example `["temperature"]` |
| `bm25_serve_threshold` | the word-overlap score (0 to 1) at which a close match is served. Default `1` |
| `bm25_review_threshold` | the floor for the review queue. Default `0.6` |

```bash
docker exec semula semula project settings support-bot --set '{"tail_n": 2}'
```

Changing a matching setting re-keys the project's existing entries, so nothing has to be re-recorded.

Next: [keep recordings in git](cassettes.md).
