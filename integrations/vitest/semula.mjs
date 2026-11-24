// semula for vitest (and any Node test runner): start a throwaway semula, point the code under
// test at it, and fail on any call that had no recorded answer.
//
//   // vitest.config.ts
//   export default { test: { globalSetup: ["./semula.setup.mjs"] } };
//
//   // semula.setup.mjs
//   import { startSemula } from "semula-vitest/semula.mjs";
//   let semula;
//   export async function setup() { semula = await startSemula({ store: "./recordings" }); }
//   export async function teardown() { await semula.assertNoMisses(); await semula.stop(); }
//
// Needs the full `semula` command on PATH (`pip install semula`); the standalone binary has no
// `serve`. No dependencies; Node 18 or later.

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/** The failure message for a miss report: each call and its nearest recorded entry. */
export function describeMisses(report) {
  const lines = [`${report.misses} call(s) had no recorded answer (${report.calls} made):`];
  for (const m of report.missed) {
    const hint = m.nearest ? `nearest ${m.nearest.entry_id} (${m.nearest.score.toFixed(2)})` : "no entries yet";
    lines.push(`  ${m.outcome}: ${m.preview || "(no preview)"}  [${hint}]`);
  }
  lines.push("Record them with `semula run --mode record -- <your command>`, then rerun.");
  return lines.join("\n");
}

/**
 * Start `semula serve` on a free port with a throwaway store (or `store`, kept between runs) and
 * set OPENAI_BASE_URL. Returns { url, baseUrl, since, check, assertNoMisses, stop }.
 * @param {{ store?: string, command?: string, record?: boolean }} [options]
 */
export async function startSemula({ store, command = "semula", record = false } = {}) {
  const scratch = store ? null : mkdtempSync(join(tmpdir(), "semula-vitest-"));
  const port = await freePort();
  const url = `http://127.0.0.1:${port}`;
  const child = spawn(command, ["serve", "--host", "127.0.0.1", "--port", String(port)], {
    env: {
      ...process.env,
      SEMULA_DATA_DIR: store ?? scratch,
      SEMULA_MODE: record ? "record" : "replay",
      SEMULA_API_KEY: "",
      SEMULA_DATABASE_URL: "",
      SEMULA_HOSTED: "off",
    },
    stdio: ["ignore", "ignore", "inherit"],
  });
  let exited = null;
  child.once("error", (e) => (exited = e));
  child.once("exit", (code) => (exited = new Error(`semula exited with code ${code}`)));

  for (let i = 0; ; i++) {
    if (exited) throw new Error(`could not start semula: ${exited.message}. Is the \`${command}\` command installed?`);
    try {
      if ((await fetch(`${url}/health`)).ok) break;
    } catch {
      /* not up yet */
    }
    if (i > 500) throw new Error("semula did not start in time");
    await sleep(20);
  }

  const since = new Date().toISOString();
  process.env.OPENAI_BASE_URL = `${url}/v1`;
  process.env.OPENAI_API_KEY ||= "semula";

  const check = async (from = since) => {
    const resp = await fetch(`${url}/api/v1/check?since=${encodeURIComponent(from)}`);
    if (!resp.ok) throw new Error(`semula check failed: ${resp.status}`);
    return resp.json();
  };
  return {
    url,
    baseUrl: `${url}/v1`,
    since,
    check,
    /** Throws, with each missed call listed, if any call had no recorded answer. */
    async assertNoMisses(from = since) {
      const report = await check(from);
      if (!report.ok) throw new Error(describeMisses(report));
    },
    async stop() {
      child.kill("SIGTERM");
      await new Promise((r) => (child.exitCode === null ? child.once("exit", r) : r()));
      if (scratch) rmSync(scratch, { recursive: true, force: true });
    },
  };
}
