import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import http from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const repoRoot = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const serverScript = join(repoRoot, "plugins", "bhm-codex-connector", "scripts", "bhm-workbench-server.mjs");
const capability = "test-workbench-capability-0123456789abcdef";

function readStartup(child) {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => reject(new Error(`Workbench startup timed out: ${stderr}`)), 10000);
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
      const newline = stdout.indexOf("\n");
      if (newline < 0) return;
      clearTimeout(timer);
      try { resolve(JSON.parse(stdout.slice(0, newline))); }
      catch (error) { reject(new Error(`Invalid Workbench startup payload: ${error}; stderr=${stderr}`)); }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`Workbench exited before startup payload: code=${code}; stderr=${stderr}`));
    });
  });
}

function request(port, path, { method = "GET", headers = {}, body = "" } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { hostname: "127.0.0.1", port, path, method, headers },
      (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
      }
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

test("Workbench rejects caller confusion before route execution", async (t) => {
  const child = spawn(process.execPath, [serverScript], {
    cwd: repoRoot,
    env: {
      ...process.env,
      BHM_WORKBENCH_PORT: "0",
      BHM_WORKBENCH_CAPABILITY: capability,
      BHM_CALLER_TOKEN: "invalid-test-caller-token",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  t.after(() => child.kill());
  const startup = await readStartup(child);
  const port = new URL(startup.url).port;

  const root = await request(port, "/");
  assert.equal(root.status, 200);
  assert.equal(root.body.includes(capability), false);
  assert.match(root.headers["content-security-policy"], /frame-ancestors 'none'/);

  const panel = await request(port, "/api/mcp-panel", {
    headers: {
      Authorization: `Bearer ${capability}`,
      Origin: `http://127.0.0.1:${port}`,
      "Sec-Fetch-Site": "same-origin",
    },
  });
  assert.equal(panel.status, 200);
  assert.equal(JSON.parse(panel.body).catalog.expected_tool_count, 35);

  const missing = await request(port, "/api/low-context", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  assert.equal(missing.status, 401);
  assert.equal(JSON.parse(missing.body).error, "workbench_capability_required");

  const badOrigin = await request(port, "/api/low-context", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${capability}`,
      "Content-Type": "application/json",
      Origin: "https://example.invalid",
      "Sec-Fetch-Site": "cross-site",
    },
    body: "{}",
  });
  assert.equal(badOrigin.status, 403);
  assert.equal(JSON.parse(badOrigin.body).error, "workbench_origin_rejected");

  const valid = await request(port, "/api/not-found", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${capability}`,
      Origin: `http://127.0.0.1:${port}`,
      "Sec-Fetch-Site": "same-origin",
    },
  });
  assert.equal(valid.status, 404);
  assert.equal(JSON.parse(valid.body).error, "not_found");
});
