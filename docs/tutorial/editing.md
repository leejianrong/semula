# Edit, pin and template

A recording is not set in stone. You can change an answer, protect it from agents, and make parts of it change on every replay.

## Templates

Real timestamps and ids in a recorded answer go stale. Write a template instead, and semula expands it fresh on every replay:

| Template | Becomes |
|---|---|
| `{{now}}` | the current time, ISO 8601 |
| `{{uuid}}` | a new UUID |
| `{{request.model}}` | the model name the app asked for |

Put one in an answer:

```bash
docker exec semula semula entry set <entry-id> --version 1 \
  --text '{"label": "hardware", "at": "{{now}}", "id": "{{uuid}}"}'
```

Call it twice. The label stays, the time and id change:

```
{"label": "hardware", "at": "2026-10-22T10:03:42.137Z", "id": "577a37ec-1103-488d-9ad4-f6e6e39234a5"}
{"label": "hardware", "at": "2026-10-22T10:03:43.260Z", "id": "daef7665-2b56-48c0-98e1-76aadef82881"}
```

## Versions

Every change needs the version you read. If someone else changed the entry in the meantime, your write is refused instead of silently overwriting theirs:

```bash
docker exec semula semula entry add support-bot \
  --prompt "Classify: my screen is cracked" --text "x"
```

```
error: entry_exists: An entry already matches this request (01M5HH1FQC65G543SK7JAABCJV). Pass its current 'version' to replace its response. (at version)
entry 01M5HH1FQC65G543SK7JAABCJV is at version 1: pass --version 1 to replace its answer
```

The exit code is `4`. Re-read the entry, look at what changed with `semula entry history <id>`, and write again with the current version. The last 50 versions of each entry are kept.

## Pins

When you have fixed an answer by hand, pin it so an agent cannot overwrite it:

```bash
docker exec semula semula entry set 01M5HH1FQC65G543SK7JAABCJV --version 1 --text "hardware" --pin
docker exec semula semula entry set 01M5HH1FQC65G543SK7JAABCJV --version 2 --text "oops"
```

```
updated 01M5HH1FQC65G543SK7JAABCJV -> version 2
error: entry_pinned: Entry 01M5HH1FQC65G543SK7JAABCJV is pinned by a human. Agents cannot change it unless forced; ask a human, or pass force=true if you were told to.
```

Writes are made as an *agent* unless you say otherwise. Add `--as-human` when it really is you:

```bash
docker exec semula semula entry set 01M5HH1FQC65G543SK7JAABCJV --version 2 --text "hardware, cracked" --as-human
docker exec semula semula entry history 01M5HH1FQC65G543SK7JAABCJV
```

```
v3	2026-10-22T10:03:34.075Z	human	answer replaced
v2	2026-10-22T10:03:31.081Z	agent	answer replaced, pinned
v1	2026-10-22T10:03:24.271Z	agent	created
```

## Several answers for one prompt

An entry can hold more than one response and pick between them by call order. Add a response with `--add`, then choose how semula picks:

```bash
docker exec semula semula entry set <entry-id> --version 3 --text "second answer" --add --selection sequence
```

`first` always returns the first, `sequence` walks the list and stays on the last, `cycle` wraps around. This is how you test a retry: first call fails, second succeeds.

Next: [near misses and the review queue](near-misses.md).
