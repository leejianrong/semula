# Keep recordings in git

Recordings are most useful when the whole team and CI share them. Export a project to a directory of JSON files, commit it, and import it anywhere.

```bash
docker exec semula semula export support-bot --dir /tmp/cassettes
```

```
exported 2 entries and 1 aliases to /tmp/cassettes
```

The layout is built for clean diffs: sorted keys, one file per entry, ids that never change.

```
cassettes/semula.json
cassettes/entries/01M5HH1FQC65G543SK7JAABCJV.json
cassettes/entries/01M5HH2TWZ5DCG5P6T7JJG3NP6.json
cassettes/aliases/01M5HH2PQQ46C7Y9H0ZGVA9E6S.json
```

`--dir` is a path inside the container. To get the files onto your machine, mount a directory or use `docker cp semula:/tmp/cassettes ./cassettes`.

## Load them somewhere else

```bash
docker exec semula semula import /tmp/cassettes
```

```
imported into 'support-bot': entries +0 ~0 =2, aliases +0 ~0 =1
```

`+` is created, `~` is updated, `=` is unchanged. Import matches by id, never deletes, and is safe to repeat. Pinned entries are left alone unless you pass `--force`, and a bundle can create a project but cannot turn recording on.

!!! warning "Read the answers before you push"
    Stored *requests* are redacted: API keys, GitHub and Slack tokens, bearer tokens and private keys become `[REDACTED]`, plus anything matching the project's `redact_patterns`. *Answers* are not redacted. Look at them before they go into a public repository.

## In CI

Start the container in replay mode, import the cassettes, and run your tests against it:

```bash
docker run -d --name semula -p 127.0.0.1:8080:8080 ghcr.io/leejianrong/semula
docker cp cassettes semula:/tmp/cassettes
docker exec semula semula import /tmp/cassettes
```

A prompt nobody recorded fails with `424` instead of reaching a paid API.

Next: [watch it work in the dashboard](dashboard.md).
