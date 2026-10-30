# Replay without a recorder

Record mode is for the first run. Once you have the recordings, you want the opposite: no waiting, and an error for anything unknown, so a test never silently invents an answer.

## Switch a project to replay

Mode is a setting of each project, and semula keeps it across restarts:

```bash
docker exec semula semula mode replay --project default
```

```text
default: replay (kept across restarts)
```

Now ask something that was never recorded:

```bash
curl -i http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Name a vegetable."}]}'
```

```text
HTTP/1.1 424 Failed Dependency

{"nearest":{"entry_id":"01M5HGZ4Y2JJHS5HCTKW5E12GT","score":0.667,"last_message":"Name a fruit."},"error":{"message":"No recorded answer matches this request, and semula is in replay mode.","type":"semula_error","param":null,"code":"semula_no_match"}}
```

The call fails at once with status 424. The body names the closest recorded prompt and its score, which is usually enough to see why a prompt you expected to match did not.

Recorded prompts keep working, with no network and no agent:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Name a fruit."}]}'
```

## Two levels of mode

`semula mode` with no arguments shows both:

```bash
docker exec semula semula mode record --project default
docker exec semula semula mode
```

```text
default: record (kept across restarts)
server default   record
project default  record
```

The *server default* starts from `SEMULA_MODE` and is forgotten on restart. A project that has not chosen its own mode follows it. A project's own choice sticks. Use `--project` for anything you want to survive a restart.

!!! warning "Imports never turn recording on"
    A project's mode is not exported, and importing a bundle cannot switch recording on. A cassette from a colleague cannot make your server start parking calls.

Next: [let an agent do the answering](agent.md).
