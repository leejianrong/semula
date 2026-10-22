# One project per app

Recordings are grouped into projects, so two apps with the same prompt can have different answers. The project `default` always exists.

```bash
docker exec semula semula project create support-bot --name "Support bot"
docker exec semula semula mode record --project support-bot
```

```
created project support-bot
support-bot: record (kept across restarts)
```

## Pick a project per call

Choose the project with a header or with the URL. Both mean the same thing:

=== "Header"

    ```bash
    curl -H 'X-Semula-Project: support-bot' \
      http://127.0.0.1:8080/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"model": "m", "messages": [{"role": "user", "content": "Hi"}]}'
    ```

=== "URL"

    ```bash
    curl http://127.0.0.1:8080/v1/projects/support-bot/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"model": "m", "messages": [{"role": "user", "content": "Hi"}]}'
    ```

With the OpenAI SDK, set `base_url="http://127.0.0.1:8080/v1/projects/support-bot"` once and forget it.

## Author an entry before any call

You do not have to wait for a miss. `entry add` writes a request and its answer up front:

```bash
docker exec semula semula entry add support-bot \
  --prompt "Classify: my screen is cracked" \
  --text '{"label": "hardware"}'
```

```
created entry 01M5HH1FQC65G543SK7JAABCJV (version 1)
```

For a request with a system prompt, tools or parameters, put the whole chat body in a file with `--request-file` and the answer in `--response-file`. `--model` and `--system` cover the common small cases.

The entry is there now, with no hits yet:

```bash
docker exec semula semula entry list support-bot
```

```
01M5HH1FQC65G543SK7JAABCJV	v1	-	hits=0	-	aliases=0	Classify: my screen is cracked
(1 of 1)
```

Next: [edit, pin and template](editing.md).
