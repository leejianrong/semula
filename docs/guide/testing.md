# Use semula in your tests

Three ways to run a test suite against semula, from least to most set-up. All of them start a throwaway server, so there is no container to keep running and no base URL to wire by hand. All of them fail on a call that had no recorded answer.

## Any command: `semula run`

```console
$ semula run --store ./recordings --mode strict -- npm test
```

The command sees `OPENAI_BASE_URL`, and `semula run` exits 1 on a miss even if the command passed. See [one command, one throwaway server](busy-queue.md#one-command-one-throwaway-server).

## pytest

Installing semula registers a pytest plugin. Ask for the `semula` fixture and the code under test talks to semula:

```python
from openai import OpenAI


def test_summary(semula):
    client = OpenAI()  # OPENAI_BASE_URL now points at semula
    out = client.chat.completions.create(
        model="semula", messages=[{"role": "user", "content": "Summarise: ..."}]
    )
    assert out.choices[0].message.content
```

After each test the fixture asks semula for the calls that had no recorded answer, and the test errors with each one and its nearest recorded entry:

```text
1 call(s) had no recorded answer (1 made):
  miss: Summarise: ...  [nearest ent_01h... (0.62)]
Record them with `semula run --mode record -- <your command>`, then rerun.
```

| To | Do |
|---|---|
| Keep recordings in the repo | `pytest --semula-store tests/recordings`, or `semula_store = tests/recordings` in `pytest.ini` |
| Record the misses instead of failing | `pytest --semula-record` (an agent answers parked calls; see [Manage a busy queue](busy-queue.md)) |
| Allow misses in one test | `@pytest.mark.semula_allow_misses` |
| Check inside a test | `semula.assert_no_misses()` |
| Reach the server | `semula.url`, `semula.base_url` |

One server runs for the whole session, and each test is judged only on its own calls.

## vitest (and other Node runners)

`integrations/vitest/semula.mjs` has no dependencies. It needs the full `semula` command on PATH (`pip install semula`), since the standalone binary has no `serve`.

```js
// semula.setup.mjs, listed under test.globalSetup in vitest.config
import { startSemula } from "./integrations/vitest/semula.mjs";

let semula;
export async function setup() {
  semula = await startSemula({ store: "./recordings" }); // sets OPENAI_BASE_URL
}
export async function teardown() {
  await semula.assertNoMisses(); // throws, listing each missed call
  await semula.stop();
}
```

`startSemula({ record: true })` records instead. Use `semula.assertNoMisses()` inside a test to check just that test: it takes the time to count from, `semula.assertNoMisses(startedAt)`.
