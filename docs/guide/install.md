# Install

semula runs three ways. Pick the one that fits.

=== "Docker"

    The image is the whole server: about 240 MB, runs as a non-root user and starts in under two seconds.

    ```bash
    docker run -d --name semula -p 127.0.0.1:8080:8080 \
      -e SEMULA_MODE=record -v semula-data:/data ghcr.io/leejianrong/semula
    ```

    Everything semula stores lives under `/data`. Keep a volume there and recordings survive upgrades and restarts. A bind mount must be writable by uid 10001 (`chown 10001 ./data`).

    Run the CLI inside the container: `docker exec semula semula pending`.

    From a clone, [`compose.yaml`](https://github.com/leejianrong/semula/blob/main/compose.yaml) is the reference setup:

    ```bash
    docker compose up -d --wait semula                          # SQLite on a named volume
    docker compose --profile postgres up -d --wait semula-pg    # the same app on Postgres
    docker compose --profile embeddings up -d --wait semula-embed  # with meaning-based matching (440 MB)
    ```

=== "CLI binary"

    A single file with no Python needed, for Linux and macOS (amd64 and arm64). Download the one for your machine from the [latest release](https://github.com/leejianrong/semula/releases/latest), check it against `SHA256SUMS`, and make it executable:

    ```bash
    sha256sum -c --ignore-missing SHA256SUMS
    chmod +x semula-*-linux-amd64 && mv semula-*-linux-amd64 ~/.local/bin/semula
    semula --url http://127.0.0.1:8080 pending
    ```

    On macOS use `shasum -a 256 -c SHA256SUMS`.

    The binary is the *client*: every command that talks to a running server works (`pending`, `answer`, `entry`, `review`, `export`, `import`, `mode`, `log`, `tail`, `test-match`). It leaves out `serve`, `mcp` and `embeddings fetch`, which need the server library. They say so and point you to the image. For an agent, use the HTTP MCP endpoint instead of `semula mcp`.

=== "Python package"

    From a clone, with [uv](https://docs.astral.sh/uv/). semula is not on PyPI yet.

    ```bash
    git clone https://github.com/leejianrong/semula && cd semula
    uv sync
    SEMULA_MODE=record uv run semula serve
    ```

    This is the install that has `semula serve` and the stdio MCP proxy `semula mcp`, for clients that cannot speak HTTP.

!!! tip "Keep it on loopback"
    `-p 127.0.0.1:8080:8080` publishes the port to your machine only. If you publish it further, set `SEMULA_API_KEY` first. See [Configuration](../reference/configuration.md#protecting-a-server).

## Check it

```bash
curl http://127.0.0.1:8080/health
```

```json
{"status":"ok","version":"0.2.0","mode":"record"}
```

Then follow the [tutorial](../tutorial/first-recording.md).
