# Your first recording

In this tutorial we start semula, send it a prompt, answer it by hand, and watch the same prompt replay for free.

You need Docker. Everything on this page runs against the published image.

## Start semula

```bash
docker run -d --name semula -p 127.0.0.1:8080:8080 \
  -e SEMULA_MODE=record -v semula-data:/data ghcr.io/leejianrong/semula
```

Three things to notice. The port is bound to `127.0.0.1`, so only your machine can reach it. `SEMULA_MODE=record` tells semula to hold prompts it has no answer for. The volume keeps your recordings when the container is replaced.

Check that it is up:

```bash
curl http://127.0.0.1:8080/health
```

```json
{"status":"ok","version":"0.4.0","mode":"record"}
```

## Call it like OpenAI

Point any OpenAI client at `http://127.0.0.1:8080/v1`. The API key can be anything.

=== "curl"

    ```bash
    curl http://127.0.0.1:8080/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Name a fruit."}]}'
    ```

=== "Python"

    ```python
    from openai import OpenAI

    client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="not-needed")
    reply = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Name a fruit."}],
    )
    print(reply.choices[0].message.content)
    ```

The call does not return. semula has never seen this prompt, so it parked the call.

## Answer it

In a second terminal:

```bash
docker exec semula semula pending
```

```text
01M5HGVZ3RCV5X9QWZKTDG9D7Z	pending	default	Name a fruit.
```

The columns are the id, the status, the project and the last message. Answer it with that id:

```bash
docker exec semula semula answer 01M5HGVZ3RCV5X9QWZKTDG9D7Z --text "Mango"
```

```text
answered 01M5HGVZ3RCV5X9QWZKTDG9D7Z -> entry 01M5HGZ4Y2JJHS5HCTKW5E12GT
```

The first terminal now prints a normal chat completion whose message is `Mango`. semula checked the answer, saved it as an *entry*, and released the call.

## Ask again

Run the same request. It returns at once, with no parked call:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Name a fruit."}]}'
```

You can see both calls in the log:

```bash
docker exec semula semula log --limit 5
```

```text
10:02:07.793  default      hit            exact 1.00  3ms  01M5HGZ4Y2JJHS5HCTKW5E12GT  "Name a fruit."
10:02:07.703  default      recorded       -  104305ms  01M5HGVZ3RCV5X9QWZKTDG9D7Z  "Name a fruit."
```

The first was `recorded` and took as long as you took to answer. The second was a `hit` on the `exact` tier and took 3 ms.

!!! tip "Different model name, same hit"
    The model name your app sends is not part of what semula matches on, so switching from `gpt-4o` to another name still hits. Settings that change the answer, like `temperature`, can be made to count per project. See [Near misses](near-misses.md).


## Recap

- Start semula in record mode with `docker run`.
- Point a client at `http://127.0.0.1:8080/v1` and send a prompt.
- Answer the parked call with `semula answer`.
- Send the prompt again to get the stored answer.

To install another way, see [Install](../guide/install.md).

Next: [replay without a recorder](replay.md).
