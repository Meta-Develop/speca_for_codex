import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { EventEmitter } from "node:events";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { runRunCommand } from "../src/commands/run.js";
import { spawnPipeline } from "../src/lib/pipeline/spawn.js";
import { startLogWatcher } from "../src/lib/pipeline/log-watcher.js";

const NODE = process.execPath;
const ts = "2026-05-03T12:00:00.000+00:00";

let cwd: string;
let scriptDir: string;
beforeEach(() => {
  cwd = mkdtempSync(join(tmpdir(), "speca-run-"));
  scriptDir = mkdtempSync(join(tmpdir(), "speca-run-script-"));
});
afterEach(() => {
  rmSync(cwd, { recursive: true, force: true });
  rmSync(scriptDir, { recursive: true, force: true });
});

/**
 * Build a fake spawnPipeline that uses Node to emit a canned NDJSON stream
 * mimicking the dependency-failure path (`speca run --phase 01b` in a cwd
 * with no upstream outputs would produce phase-failed + exit 1).
 *
 * We materialise the fixture as a real .mjs file rather than passing it via
 * `node -e` because Node 22+ may attempt to evaluate the payload as
 * TypeScript and reject embedded escapes.
 */
function fakeSpawnDependencyFailure(): typeof spawnPipeline {
  return ((opts) => {
    const phases = opts.phases ?? [opts.target ?? "01b"];
    const event1 = {
      type: "pipeline-started",
      ts,
      phases,
      workers: opts.workers ?? 4,
      max_concurrent: opts.maxConcurrent ?? 8,
      force: opts.force ?? false,
    };
    const event2 = {
      type: "phase-failed",
      ts,
      phase: phases[0],
      reason: "dependency check failed",
      duration_s: 0.1,
    };
    const event3 = {
      type: "pipeline-completed",
      ts,
      phases,
      results: { [phases[0]!]: false },
      duration_s: 0.2,
    };
    const body = `const events = ${JSON.stringify([event1, event2, event3])};
for (const e of events) process.stdout.write(JSON.stringify(e) + "\\n");
process.exit(1);
`;
    const path = join(scriptDir, `fake-${Math.random().toString(36).slice(2)}.mjs`);
    writeFileSync(path, body, "utf8");
    return spawnPipeline({
      ...opts,
      command: NODE,
      baseArgs: [path],
    });
  }) as typeof spawnPipeline;
}

function fakeSpawnSuccess(seen?: Parameters<typeof spawnPipeline>[0][]): typeof spawnPipeline {
  return ((opts) => {
    seen?.push(opts);
    const handle = new EventEmitter() as ReturnType<typeof spawnPipeline>;
    Object.defineProperty(handle, "done", { value: Promise.resolve(0) });
    Object.defineProperty(handle, "pid", { value: 123 });
    handle.stop = () => false;
    handle.kill = () => false;
    return handle;
  }) as typeof spawnPipeline;
}

async function withStdoutTTY<T>(isTTY: boolean, fn: () => Promise<T>): Promise<T> {
  const original = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
  Object.defineProperty(process.stdout, "isTTY", { value: isTTY, configurable: true });
  try {
    return await fn();
  } finally {
    if (original) {
      Object.defineProperty(process.stdout, "isTTY", original);
    } else {
      Reflect.deleteProperty(process.stdout, "isTTY");
    }
  }
}

function writeExpiredAnthropicAuth(authFile: string): void {
  const now = Date.now();
  writeFileSync(
    authFile,
    JSON.stringify({
      version: 1,
      accounts: {
        default: {
          type: "oauth",
          access_token: "a",
          refresh_token: "r",
          expires_at: now - 60_000,
          scopes: ["user:sessions:claude_code"],
          created_at: now - 3_600_000,
        },
      },
    }),
  );
}

describe("runRunCommand — headless flow", () => {
  it("returns an error code when neither --phase nor --target is provided", async () => {
    const code = await runRunCommand({ flags: {}, cwd });
    expect(code).toBe(2);
  });

  it("allows --list-runtimes without --phase or --target and forces headless", async () => {
    const seen: Parameters<typeof spawnPipeline>[0][] = [];
    let startLogsCalled = false;

    const code = await withStdoutTTY(true, () =>
      runRunCommand({
        flags: { listRuntimes: true },
        cwd,
        authFile: join(cwd, "no-such-auth.json"),
        spawn: fakeSpawnSuccess(seen),
        startLogs: async () => {
          startLogsCalled = true;
          return async () => {};
        },
      }),
    );

    expect(code).toBe(0);
    expect(startLogsCalled).toBe(false);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({ listRuntimes: true });
    expect(seen[0]?.phases).toBeUndefined();
    expect(seen[0]?.target).toBeUndefined();
  });

  it("fakes a dependency-failure NDJSON stream and exits non-zero", async () => {
    // Force headless mode by passing --no-tui (also avoids stdout.isTTY confusion).
    const code = await runRunCommand({
      flags: { phase: ["01b"], noTui: true },
      cwd,
      spawn: fakeSpawnDependencyFailure(),
      // Skip log watcher in tests (it tries to mkdir; allowed but unnecessary here).
      startLogs: async () => async () => {},
      skipPreflight: true,
    });
    expect(code).toBe(1);
  });

  it("forwards --runtime to spawnPipeline options", async () => {
    const seen: Parameters<typeof spawnPipeline>[0][] = [];

    const code = await runRunCommand({
      flags: { target: "04", runtime: "codex", noTui: true },
      cwd,
      spawn: fakeSpawnSuccess(seen),
      startLogs: async () => async () => {},
      skipPreflight: true,
    });

    expect(code).toBe(0);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({ target: "04", runtime: "codex" });
  });

  it("--json mode emits one JSON object per line on stdout", async () => {
    const lines: string[] = [];
    const origWrite = process.stdout.write.bind(process.stdout);
    process.stdout.write = ((chunk: string | Uint8Array): boolean => {
      const text = typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8");
      lines.push(text);
      return true;
    }) as typeof process.stdout.write;
    try {
      const code = await runRunCommand({
        flags: { phase: ["01b"], json: true },
        cwd,
        spawn: fakeSpawnDependencyFailure(),
        startLogs: async () => async () => {},
        skipPreflight: true,
      });
      expect(code).toBe(1);
    } finally {
      process.stdout.write = origWrite;
    }
    const joined = lines.join("");
    const events = joined.split("\n").filter(Boolean).map((l) => JSON.parse(l));
    expect(events).toHaveLength(3);
    expect(events[0].type).toBe("pipeline-started");
    expect(events[1].type).toBe("phase-failed");
    expect(events[2].type).toBe("pipeline-completed");
  });

  it("falls back to startLogs default without crashing if a watcher fails", async () => {
    // Pass a startLogs that throws — the run should still complete.
    const code = await runRunCommand({
      flags: { phase: ["01b"], noTui: true },
      cwd,
      spawn: fakeSpawnDependencyFailure(),
      startLogs: (async () => {
        throw new Error("watcher unavailable");
      }) as typeof startLogWatcher,
      skipPreflight: true,
    });
    expect(code).toBe(1);
  });
});

describe("runRunCommand — pre-flight checks (#28 ErrorKind wiring)", () => {
  let stderrCapture: { chunks: string[]; restore(): void };

  beforeEach(() => {
    const chunks: string[] = [];
    const orig = process.stderr.write.bind(process.stderr);
    process.stderr.write = ((c: string | Uint8Array): boolean => {
      chunks.push(typeof c === "string" ? c : Buffer.from(c).toString("utf8"));
      return true;
    }) as typeof process.stderr.write;
    stderrCapture = {
      chunks,
      restore() {
        process.stderr.write = orig;
      },
    };
  });
  afterEach(() => {
    stderrCapture.restore();
  });

  it("exits with auth-expired when the only OAuth token is past expiry", async () => {
    const authFile = join(cwd, "auth.json");
    writeExpiredAnthropicAuth(authFile);

    const code = await runRunCommand({
      flags: { phase: ["01b"], noTui: true },
      cwd,
      authFile,
      // Pre-flight should fire before spawn — these stay defaults / unused.
    });
    expect(code).toBe(1);
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).toContain("kind=auth-expired");
    expect(stderr.toLowerCase()).toContain("expired");
  });

  it("skips expired Anthropic auth pre-flight for non-Claude runtimes and spawns", async () => {
    const authFile = join(cwd, "auth.json");
    writeExpiredAnthropicAuth(authFile);
    const seen: Parameters<typeof spawnPipeline>[0][] = [];

    const code = await runRunCommand({
      flags: { phase: ["01b"], runtime: "codex", noTui: true },
      cwd,
      authFile,
      spawn: fakeSpawnSuccess(seen),
      startLogs: async () => async () => {},
    });

    expect(code).toBe(0);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({ phases: ["01b"], runtime: "codex" });
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).not.toContain("kind=auth-expired");
  });

  it("still applies stale-resume pre-flight for non-Claude runtimes", async () => {
    const authFile = join(cwd, "auth.json");
    writeExpiredAnthropicAuth(authFile);
    const outputsDir = join(cwd, "outputs");
    require("node:fs").mkdirSync(outputsDir, { recursive: true });
    const partialPath = join(outputsDir, "01b_PARTIAL_W0B0.json");
    writeFileSync(partialPath, "{}");
    const targetPath = join(outputsDir, "TARGET_INFO.json");
    writeFileSync(targetPath, "{}");
    const partialMtime = Date.now() / 1000 - 600;
    const targetMtime = Date.now() / 1000;
    require("node:fs").utimesSync(partialPath, partialMtime, partialMtime);
    require("node:fs").utimesSync(targetPath, targetMtime, targetMtime);
    const seen: Parameters<typeof spawnPipeline>[0][] = [];

    const code = await runRunCommand({
      flags: { phase: ["01b"], runtime: "codex", noTui: true },
      cwd,
      authFile,
      spawn: fakeSpawnSuccess(seen),
      startLogs: async () => async () => {},
    });

    expect(code).toBe(1);
    expect(seen).toHaveLength(0);
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).not.toContain("kind=auth-expired");
    expect(stderr).toContain("kind=stale-resume");
  });

  it("exits with stale-resume when TARGET_INFO is much newer than the newest 01b partial", async () => {
    const outputsDir = join(cwd, "outputs");
    require("node:fs").mkdirSync(outputsDir, { recursive: true });
    const partialPath = join(outputsDir, "01b_PARTIAL_W0B0.json");
    writeFileSync(partialPath, "{}");
    const targetPath = join(outputsDir, "TARGET_INFO.json");
    writeFileSync(targetPath, "{}");

    // Make TARGET_INFO 10 minutes newer than the partial.
    const partialMtime = Date.now() / 1000 - 600;
    const targetMtime = Date.now() / 1000;
    require("node:fs").utimesSync(partialPath, partialMtime, partialMtime);
    require("node:fs").utimesSync(targetPath, targetMtime, targetMtime);

    const code = await runRunCommand({
      flags: { phase: ["01b"], noTui: true },
      cwd,
      // Need a non-existent auth file so detectExpiredAuth returns null
      // (otherwise the dev's real auth.json on a non-test machine could
      // mask the stale-resume signal).
      authFile: join(cwd, "no-such-auth.json"),
    });
    expect(code).toBe(1);
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).toContain("kind=stale-resume");
  });

  it("warns to stderr when --phase contains an id outside KNOWN_PHASE_IDS", async () => {
    const code = await runRunCommand({
      flags: { phase: ["bogus-phase-99"], noTui: true },
      cwd,
      authFile: join(cwd, "no-such-auth.json"),
      spawn: fakeSpawnDependencyFailure(),
      startLogs: async () => async () => {},
      skipPreflight: true,
    });
    // Spawn proceeds (forks may legitimately add phases) — exit reflects
    // the fake's exit code, but the user got the heads-up.
    expect(code).toBe(1);
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).toContain("phase id(s) not in the canonical set");
    expect(stderr).toContain("bogus-phase-99");
  });

  it("does NOT warn when every --phase id is in KNOWN_PHASE_IDS", async () => {
    const code = await runRunCommand({
      flags: { phase: ["01a", "01b"], noTui: true },
      cwd,
      authFile: join(cwd, "no-such-auth.json"),
      spawn: fakeSpawnDependencyFailure(),
      startLogs: async () => async () => {},
      skipPreflight: true,
    });
    expect(code).toBe(1);
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).not.toContain("phase id(s) not in the canonical set");
  });

  it("--force overrides the stale-resume detector and proceeds to spawn", async () => {
    const outputsDir = join(cwd, "outputs");
    require("node:fs").mkdirSync(outputsDir, { recursive: true });
    writeFileSync(join(outputsDir, "01b_PARTIAL_W0B0.json"), "{}");
    writeFileSync(join(outputsDir, "TARGET_INFO.json"), "{}");
    const old = Date.now() / 1000 - 600;
    const now = Date.now() / 1000;
    require("node:fs").utimesSync(join(outputsDir, "01b_PARTIAL_W0B0.json"), old, old);
    require("node:fs").utimesSync(join(outputsDir, "TARGET_INFO.json"), now, now);

    const code = await runRunCommand({
      flags: { phase: ["01b"], noTui: true, force: true },
      cwd,
      authFile: join(cwd, "no-such-auth.json"),
      spawn: fakeSpawnDependencyFailure(),
      startLogs: async () => async () => {},
    });
    // We pass --force, so stale-resume is skipped and the fake spawn
    // proceeds to its dependency-failure exit (1).
    expect(code).toBe(1);
    const stderr = stderrCapture.chunks.join("");
    expect(stderr).not.toContain("kind=stale-resume");
  });
});
