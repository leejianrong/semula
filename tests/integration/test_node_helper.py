"""The vitest/Node helper (SEM-107), driven with node's own `node --test` (no vitest needed)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
HELPER = Path(__file__).parents[2] / "integrations" / "vitest" / "semula.mjs"
SCRIPT = """
import { startSemula } from "%s";
const semula = await startSemula({ command: process.argv[2] });
const ask = (text) => fetch(process.env.OPENAI_BASE_URL + "/chat/completions", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ model: "semula", messages: [{ role: "user", content: text }] }) });
if (process.argv[3] === "hit") {
  await fetch(semula.url + "/api/v1/projects/default/entries", { method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ request: { messages: [{ role: "user", content: "hello" }] },
                           response: { content: "hi" } }) });
  await ask("hello");
} else {
  await ask("a prompt nobody recorded");
}
try { await semula.assertNoMisses(); console.log("NO MISSES"); }
catch (e) { console.log(e.message); process.exitCode = 3; }
await semula.stop();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize(
    ("case", "code", "text"), [("hit", 0, "NO MISSES"), ("miss", 3, "a prompt nobody recorded")]
)
def test_the_helper_fails_on_a_miss_and_passes_on_a_hit(
    tmp_path: Path, case: str, code: int, text: str
) -> None:
    script = tmp_path / "run.mjs"
    script.write_text(SCRIPT % HELPER)
    semula = str(Path(sys.executable).parent / "semula")
    done = subprocess.run(  # noqa: S603 - node and the project's own semula
        [shutil.which("node") or "node", str(script), semula, case],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == code, done.stdout + done.stderr
    assert text in done.stdout
