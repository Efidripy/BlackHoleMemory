import http from "node:http";
import { timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const port = Number(process.env.BHM_WORKBENCH_PORT || 0);
const workbenchCapability = String(process.env.BHM_WORKBENCH_CAPABILITY || "");
const bhmCallerToken = String(process.env.BHM_CALLER_TOKEN || "");
if (workbenchCapability.length < 32) {
  throw new Error("BHM_WORKBENCH_CAPABILITY must be a per-launch secret of at least 32 characters");
}
const childProcessEnv = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => key !== "BHM_WORKBENCH_CAPABILITY")
);
const scriptDir = dirname(fileURLToPath(import.meta.url));
const pluginRoot = dirname(scriptDir);
const uiPath = join(pluginRoot, "ui", "bhm-workbench.html");
const profileScript = join(scriptDir, "bhm-profile.ps1");
const liveCheckScript = join(scriptDir, "bhm-run-live-memory-check.ps1");
const preflightScript = join(scriptDir, "bhm-memory-preflight.ps1");
const checkpointScript = join(scriptDir, "bhm-memory-checkpoint.ps1");
const sessionRecordScript = join(scriptDir, "bhm-session-hybrid-record.ps1");
const lessonScript = join(scriptDir, "bhm-memory-lesson.ps1");
const slotScript = join(scriptDir, "bhm-memory-slot.ps1");
const verifyScript = join(scriptDir, "bhm-memory-verify.ps1");
const timelineScript = join(scriptDir, "bhm-memory-timeline.ps1");
const auditScript = join(scriptDir, "bhm-memory-audit.ps1");
const crystallizeScript = join(scriptDir, "bhm-memory-crystallize.ps1");
const reflectScript = join(scriptDir, "bhm-memory-reflect.ps1");
const profileViewScript = join(scriptDir, "bhm-memory-profile-view.ps1");
const obsidianExportScript = join(scriptDir, "bhm-memory-obsidian-export.ps1");
const doctorActivateScript = join(scriptDir, "bhm-doctor-activate.ps1");
const portableDoctorScript = join(scriptDir, "bhm-portable-doctor.ps1");
const runtimeConfigPath = join(pluginRoot, "config", "runtime-discovery.json");
const defaultEnvPath = "C:/Users/xman/.bhm/.env";
const runtimeHints = {
  apiDefault: process.env.BHM_BASE_URL || "",
  viewerDefault: process.env.BHM_VIEWER_URL || process.env.BHM_BASE_URL || "",
  engineDefault: process.env.BHM_ENGINE_URL || "",
  otelDefault: process.env.BHM_OTEL_URL || "",
};
const MCP_PANEL_SCHEMA_VERSION = "bhm.mcp.panel.v1";

function stableSlug(value, fallback) {
  const slug = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || fallback;
}

function workflowCloseKey(project, title) {
  return `workflow-close:${stableSlug(project, "project")}:${stableSlug(title, "workbench-closeout")}`;
}

function loadRuntimeConfig() {
  if (!existsSync(runtimeConfigPath)) {
    return {
      envPaths: [defaultEnvPath],
      apiCandidates: [runtimeHints.apiDefault],
      viewerCandidates: [runtimeHints.viewerDefault].filter(Boolean),
      engineCandidates: [runtimeHints.engineDefault],
      otelCandidates: [runtimeHints.otelDefault],
    };
  }

  try {
    return JSON.parse(readFileSync(runtimeConfigPath, "utf8"));
  } catch {
    return {
      envPaths: [defaultEnvPath],
      apiCandidates: [runtimeHints.apiDefault],
      viewerCandidates: [runtimeHints.viewerDefault].filter(Boolean),
      engineCandidates: [runtimeHints.engineDefault],
      otelCandidates: [runtimeHints.otelDefault],
    };
  }
}

function expandEnvPath(value) {
  if (!value) return null;
  return value.replace(/%([^%]+)%/g, (_, key) => process.env[key] || `%${key}%`);
}

function securityHeaders(contentType) {
  return {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  };
}

function json(res, status, data) {
  res.writeHead(status, securityHeaders("application/json; charset=utf-8"));
  res.end(JSON.stringify(data, null, 2));
}

function isLoopbackHostname(value) {
  const normalized = String(value || "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "127.0.0.1" || normalized === "localhost" || normalized === "::1";
}

function loopbackHostHeaderIsValid(value) {
  try {
    const parsed = new URL(`http://${String(value || "")}`);
    return isLoopbackHostname(parsed.hostname);
  } catch {
    return false;
  }
}

function sameOriginHeaderIsValid(value, requestPort) {
  if (!value) return true;
  try {
    const parsed = new URL(String(value));
    const originPort = parsed.port || (parsed.protocol === "http:" ? "80" : parsed.protocol === "https:" ? "443" : "");
    return parsed.protocol === "http:" && isLoopbackHostname(parsed.hostname) && originPort === String(requestPort);
  } catch {
    return false;
  }
}

function capabilityMatches(authorization) {
  const prefix = "Bearer ";
  const value = String(authorization || "");
  if (!value.startsWith(prefix)) return false;
  const supplied = Buffer.from(value.slice(prefix.length), "utf8");
  const expected = Buffer.from(workbenchCapability, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function bhmCallerHeaders() {
  if (bhmCallerToken.length < 32) {
    throw new Error("BHM_CALLER_TOKEN is unavailable to the Workbench server");
  }
  return { Authorization: `Bearer ${bhmCallerToken}` };
}

function authorizeApiRequest(req, requestPort) {
  if (!loopbackHostHeaderIsValid(req.headers.host)) {
    return { status: 403, error: "workbench_host_rejected" };
  }
  if (!capabilityMatches(req.headers.authorization)) {
    return { status: 401, error: "workbench_capability_required" };
  }
  if (!sameOriginHeaderIsValid(req.headers.origin, requestPort)) {
    return { status: 403, error: "workbench_origin_rejected" };
  }
  const fetchSite = String(req.headers["sec-fetch-site"] || "").toLowerCase();
  if (fetchSite && fetchSite !== "same-origin" && fetchSite !== "none") {
    return { status: 403, error: "workbench_fetch_site_rejected" };
  }
  return null;
}

function runPowerShell(args) {
  return new Promise((resolve) => {
    const child = spawn("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", ...args], {
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: childProcessEnv,
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString("utf8"); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
    child.on("close", (code) => {
      resolve({
        ok: code === 0,
        exitCode: code,
        stdout: stdout.trim(),
        stderr: stderr.trim(),
      });
    });
  });
}

async function runJsonPowerShell(scriptPath, extraArgs = []) {
  const result = await runPowerShell(["-File", scriptPath, ...extraArgs, "-AsJson"]);
  if (!result.ok) {
    return {
      ok: false,
      exitCode: result.exitCode,
      stderr: result.stderr,
      stdout: result.stdout,
    };
  }

  try {
    return {
      ok: true,
      data: JSON.parse(result.stdout || "{}"),
    };
  } catch (error) {
    return {
      ok: false,
      parseError: String(error),
      stdout: result.stdout,
      stderr: result.stderr,
    };
  }
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk.toString("utf8"); });
    req.on("end", () => {
      if (!body.trim()) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(body));
      } catch (error) {
        reject(error);
      }
    });
    req.on("error", reject);
  });
}

function parseEnvFile(filePath) {
  if (!existsSync(filePath)) {
    return { ok: false, path: filePath, values: {}, error: "env_missing" };
  }

  const content = readFileSync(filePath, "utf8");
  const values = {};
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const idx = line.indexOf("=");
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    values[key] = value;
  }
  return { ok: true, path: filePath, values };
}

async function probeHttp(url) {
  try {
    const response = await fetch(url, { method: "GET" });
    return {
      ok: response.ok,
      status: response.status,
      statusText: response.statusText,
      url,
    };
  } catch (error) {
    return {
      ok: false,
      status: null,
      statusText: String(error),
      url,
    };
  }
}

async function firstHealthyApi(candidates = []) {
  for (const candidate of candidates) {
    const probe = await probeHttp(`${candidate.replace(/\/$/, "")}/bhm/health`);
    if (probe.ok && probe.status === 200) {
      return candidate;
    }
  }
  return candidates[0] || runtimeHints.apiDefault;
}

async function firstHealthyUrl(candidates = []) {
  for (const candidate of candidates) {
    const probe = await probeHttp(candidate);
    if (probe.ok && probe.status === 200) {
      return candidate;
    }
  }
  return candidates[0] || null;
}

async function probeRuntimeDiscovery() {
  const runtimeConfig = loadRuntimeConfig();
  const envCandidate = (runtimeConfig.envPaths || [])
    .map(expandEnvPath)
    .find((candidate) => candidate && existsSync(candidate)) || defaultEnvPath;
  const envInfo = parseEnvFile(envCandidate);
  const env = envInfo.values || {};

  const apiCandidates = [
    env.BHM_WORKSPACE_MEMORY_URL || null,
    env.III_REST_PORT ? `http://localhost:${env.III_REST_PORT}` : null,
    ...(runtimeConfig.apiCandidates || [])
  ].filter(Boolean);
  const viewerCandidates = [
    ...(runtimeConfig.viewerCandidates || [])
  ].filter(Boolean);
  const engineCandidates = [
    env.III_ENGINE_URL || null,
    ...(runtimeConfig.engineCandidates || [])
  ].filter(Boolean);
  const otelCandidates = [
    ...(runtimeConfig.otelCandidates || []),
    ...(engineCandidates.filter(Boolean).map((value) => value.endsWith("/otel") ? value : `${value}/otel`))
  ].filter(Boolean);

  const apiUrl = await firstHealthyApi(apiCandidates);
  const viewerUrl = await firstHealthyUrl(viewerCandidates);
  const engineUrl = engineCandidates[0] || runtimeHints.engineDefault;
  const otelUrl = otelCandidates[0] || (engineUrl.endsWith("/otel") ? engineUrl : `${engineUrl}/otel`);
  const llmBaseUrl = env.OPENAI_BASE_URL || null;

  const probes = {
    api: await probeHttp(apiUrl),
    livez: await probeHttp(`${apiUrl.replace(/\/$/, "")}/livez`),
      health: await probeHttp(`${apiUrl.replace(/\/$/, "")}/bhm/health`),
    viewer: await probeHttp(viewerUrl),
  };

  const resolved = {
    api_url: apiUrl,
    viewer_url: viewerUrl,
    engine_url: engineUrl,
    otel_url: otelUrl,
    llm_base_url: llmBaseUrl,
    env_path: envInfo.path,
    source: {
      api: env.BHM_WORKSPACE_MEMORY_URL ? "env.BHM_WORKSPACE_MEMORY_URL" : "default_or_III_REST_PORT",
      viewer: "default",
      engine: env.III_ENGINE_URL ? "env.III_ENGINE_URL" : "default",
      llm: env.OPENAI_BASE_URL ? "env.OPENAI_BASE_URL" : "none",
    },
  };

  const verdict = {
    runtime_health: probes.health.ok ? "healthy" : "missing",
    viewer_health: probes.viewer.ok ? "healthy" : "missing",
    root_api: probes.api.ok ? "ok" : probes.api.status === 404 ? "route-root-404" : "unhealthy",
    recommendation: probes.health.ok && probes.viewer.ok ? "runtime_usable" : "investigate_runtime",
  };

  return {
    ok: true,
    action: "runtime-discovery",
    env: {
      loaded: envInfo.ok,
      path: envInfo.path,
      values: {
        BHM_WORKSPACE_MEMORY_URL: env.BHM_WORKSPACE_MEMORY_URL || null,
        III_REST_PORT: env.III_REST_PORT || null,
        III_ENGINE_URL: env.III_ENGINE_URL || null,
        OPENAI_BASE_URL: env.OPENAI_BASE_URL || null,
        BHM_TOOLS: env.BHM_TOOLS || null,
      },
    },
    hints: runtimeHints,
    config: runtimeConfig,
    resolved,
    probes,
    verdict,
  };
}

function unavailableMcpPanel(reason = "runtime_api_unavailable") {
  return {
    schema_version: MCP_PANEL_SCHEMA_VERSION,
    read_only: true,
    writes_live_state: false,
    bounded: true,
    configured: { state: "unavailable", source_count: 0, configured_count: 0, sources: [] },
    connected: { state: "unknown", attached_count: 0, pending_count: 0, client_versions: [], protocol_state: "unknown" },
    catalog: { state: "unverified", expected_tool_count: 35, observed_tool_count: 0, generation: null, catalog_hash: null, generation_count: 0 },
    runtime: { state: "unavailable", ready: false, cutover: false, slo: "unavailable", provider_ready: false },
    errors: { last_error: { state: "unavailable", reason: String(reason).slice(0, 120), at: new Date().toISOString() }, last_reconnect: null },
    schema_drift: { state: "unverified", reason_code: "no_live_native_catalog", generation_count: 0 },
    rest_degraded: {
      status: "MCP unavailable",
      degraded: true,
      mcp_available: false,
      attached: false,
      current_session_verified: false,
      runtime_lease_live: false,
      transport_ready: false,
      streamable_http_ready: false,
      reason_code: "mcp_panel_unreachable",
      recovery_action: "restore runtime and the canonical BHM transport, then re-probe; reload only after a healthy native probe fails",
    },
    overall: { state: "unavailable", reason_code: "mcp_panel_unreachable", false_green_prevented: true, gates: {} },
  };
}

async function fetchMcpPanel(apiUrl) {
  const target = `${String(apiUrl || runtimeHints.apiDefault).replace(/\/$/, "")}/bhm/telemetry/mcp-panel`;
  try {
    const response = await fetch(target, { method: "GET", headers: bhmCallerHeaders() });
    const payload = await response.json();
    if (!response.ok || !payload || typeof payload !== "object") {
      return unavailableMcpPanel(`http_${response.status}`);
    }
    return payload;
  } catch (error) {
    return unavailableMcpPanel(String(error).slice(0, 120));
  }
}

async function fetchMcpRepair(apiUrl, operation = "preview") {
  const safeOperation = operation === "reprobe" ? "reprobe" : "preview";
  const target = `${String(apiUrl || runtimeHints.apiDefault).replace(/\/$/, "")}/bhm/mcp/repair/${safeOperation}`;
  try {
    const response = await fetch(target, { method: "GET", headers: bhmCallerHeaders() });
    const payload = await response.json();
    if (!response.ok || !payload || typeof payload !== "object") {
      return {
        schema_version: "bhm.mcp.repair.v1",
        operation: safeOperation,
        ok: false,
        read_only: true,
        writes_live_state: false,
        bounded: true,
        error: `http_${response.status}`,
      };
    }
    return payload;
  } catch (error) {
    return {
      schema_version: "bhm.mcp.repair.v1",
      operation: safeOperation,
      ok: false,
      read_only: true,
      writes_live_state: false,
      bounded: true,
      error: String(error).slice(0, 120),
    };
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || `127.0.0.1:${port}`}`);
  const address = server.address();
  const requestPort = typeof address === "object" && address ? address.port : port;

  if ((req.method === "GET" || req.method === "HEAD") && url.pathname === "/") {
    const html = await readFile(uiPath, "utf8");
    res.writeHead(200, {
      ...securityHeaders("text/html; charset=utf-8"),
      "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    });
    res.end(req.method === "HEAD" ? "" : html);
    return;
  }

  if (url.pathname.startsWith("/api/")) {
    const authorizationFailure = authorizeApiRequest(req, requestPort);
    if (authorizationFailure) {
      return json(res, authorizationFailure.status, { ok: false, error: authorizationFailure.error });
    }
  }

  if (req.method === "GET" && url.pathname === "/api/status") {
    return json(res, 200, await runJsonPowerShell(profileScript, ["-Action", "status"]));
  }

  if (req.method === "GET" && url.pathname === "/api/runtime-discovery") {
    return json(res, 200, await probeRuntimeDiscovery());
  }

  if (req.method === "GET" && url.pathname === "/api/mcp-panel") {
    const discovery = await probeRuntimeDiscovery();
    const apiUrl = discovery?.resolved?.api_url || runtimeHints.apiDefault;
    return json(res, 200, await fetchMcpPanel(apiUrl));
  }

  if (req.method === "GET" && url.pathname === "/api/mcp-repair/preview") {
    const discovery = await probeRuntimeDiscovery();
    const apiUrl = discovery?.resolved?.api_url || runtimeHints.apiDefault;
    return json(res, 200, await fetchMcpRepair(apiUrl, "preview"));
  }

  if (req.method === "GET" && url.pathname === "/api/mcp-repair/reprobe") {
    const discovery = await probeRuntimeDiscovery();
    const apiUrl = discovery?.resolved?.api_url || runtimeHints.apiDefault;
    return json(res, 200, await fetchMcpRepair(apiUrl, "reprobe"));
  }

  if (req.method === "POST" && url.pathname === "/api/low-context") {
    return json(res, 200, await runJsonPowerShell(profileScript, ["-Action", "low-context", "-RestartWorker"]));
  }

  if (req.method === "POST" && url.pathname === "/api/standard") {
    return json(res, 200, await runJsonPowerShell(profileScript, ["-Action", "standard", "-RestartWorker"]));
  }

  if (req.method === "POST" && url.pathname === "/api/compare") {
    return json(res, 200, await runJsonPowerShell(profileScript, ["-Action", "compare"]));
  }

  if (req.method === "POST" && url.pathname === "/api/live-check") {
    return json(
      res,
      200,
      await runJsonPowerShell(liveCheckScript, ["-Project", "e-github-workspace", "-Title", "bhm-ui-check"])
    );
  }

  if (req.method === "POST" && url.pathname === "/api/doctor-activate") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace",
      "-Title", body.title || "bhm-doctor-activate"
    ];
    if (body.lightweight !== false) {
      args.push("-Lightweight");
    }
    const result = await runJsonPowerShell(doctorActivateScript, args);
    if (result?.ok && result?.data) {
      result.data.action = "bhm-doctor-activate-v2";
    }
    return json(res, 200, result);
  }

  if (req.method === "POST" && url.pathname === "/api/portable-doctor") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace",
      "-Title", body.title || "bhm-portable-doctor"
    ];
    const result = await runJsonPowerShell(portableDoctorScript, args);
    if (result?.ok && result?.data) {
      result.data.action = "bhm-portable-doctor";
    }
    return json(res, 200, result);
  }

  if (req.method === "POST" && url.pathname === "/api/start-task-ritual") {
    const body = await readJsonBody(req);
    const project = body.project || "e-github-workspace";
    const status = await runJsonPowerShell(profileScript, ["-Action", "status"]);
    const discovery = await probeRuntimeDiscovery();
    const preflight = await runJsonPowerShell(preflightScript, ["-Project", project]);
    return json(res, 200, {
      ok: true,
      action: "start-task-ritual",
      project,
      status: status.data || status,
      discovery,
      preflight: preflight.data || preflight,
    });
  }

  if (req.method === "POST" && url.pathname === "/api/close-task-ritual") {
    const body = await readJsonBody(req);
    const project = body.project || "e-github-workspace";
    const done = body.done || "";
    const next = body.next || "";
    const checks = body.checks || "";
    const risks = body.risks || "";
    const title = body.title || "workbench-closeout";
    const upsertKey = body.upsertKey || workflowCloseKey(project, title);
    const checkpoint = await runJsonPowerShell(checkpointScript, [
      "-Project", project,
      "-Type", body.type || "workflow",
      "-Title", title,
      "-Done", done,
      "-Next", next,
      "-Checks", checks,
      "-Risks", risks,
      "-UpsertKey", upsertKey,
    ]);
    const session = await runJsonPowerShell(sessionRecordScript, [
      "-Project", project,
      "-Title", title,
      "-Done", done,
      "-Next", next,
      "-Checks", checks,
      "-Risks", risks,
      "-Decisions", body.decisions || "",
      "-FilesTouched", body.filesTouched || "",
      "-ConversationNotes", body.conversationNotes || "",
      "-UpsertKey", upsertKey,
    ]);
    return json(res, 200, {
      ok: true,
      action: "close-task-ritual",
      project,
      checkpoint: checkpoint.data || checkpoint,
      session_record: session.data || session,
    });
  }

  if (req.method === "POST" && url.pathname === "/api/preflight") {
    const body = await readJsonBody(req);
    const project = body.project || "e-github-workspace";
    return json(res, 200, await runJsonPowerShell(preflightScript, ["-Project", project]));
  }

  if (req.method === "POST" && url.pathname === "/api/checkpoint") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace",
      "-Type", body.type || "workflow",
      "-Done", body.done || "",
      "-Next", body.next || "",
      "-Checks", body.checks || "",
      "-Risks", body.risks || ""
    ];
    return json(res, 200, await runJsonPowerShell(checkpointScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/session-record") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace",
      "-Title", body.title || "workbench-session",
      "-Done", body.done || "",
      "-Next", body.next || "",
      "-Checks", body.checks || "",
      "-Risks", body.risks || "",
      "-Decisions", body.decisions || "",
      "-FilesTouched", body.filesTouched || "",
      "-ConversationNotes", body.conversationNotes || ""
    ];
    return json(res, 200, await runJsonPowerShell(sessionRecordScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/lesson-save") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", "save",
      "-Project", body.project || "e-github-workspace",
      "-Content", body.content || "",
      "-Context", body.context || "",
      "-Tags", body.tags || ""
    ];
    return json(res, 200, await runJsonPowerShell(lessonScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/lesson-recall") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", "recall",
      "-Project", body.project || "e-github-workspace",
      "-Query", body.query || ""
    ];
    return json(res, 200, await runJsonPowerShell(lessonScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/lesson-strengthen") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", "strengthen",
      "-Project", body.project || "e-github-workspace",
      "-LessonId", body.lessonId || ""
    ];
    return json(res, 200, await runJsonPowerShell(lessonScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/slot-list") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", "list",
      "-Project", body.project || "e-github-workspace"
    ];
    return json(res, 200, await runJsonPowerShell(slotScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/slot-get") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", "get",
      "-Project", body.project || "e-github-workspace",
      "-Label", body.label || ""
    ];
    return json(res, 200, await runJsonPowerShell(slotScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/slot-replace") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", "replace",
      "-Project", body.project || "e-github-workspace",
      "-Label", body.label || "",
      "-Content", body.content || ""
    ];
    return json(res, 200, await runJsonPowerShell(slotScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/verify") {
    const body = await readJsonBody(req);
    const args = [
      "-Id", body.id || "",
      "-Project", body.project || "e-github-workspace"
    ];
    return json(res, 200, await runJsonPowerShell(verifyScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/timeline") {
    const body = await readJsonBody(req);
    const args = [
      "-Anchor", body.anchor || "",
      "-Project", body.project || "e-github-workspace",
      "-Before", String(body.before ?? 5),
      "-After", String(body.after ?? 5)
    ];
    return json(res, 200, await runJsonPowerShell(timelineScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/audit") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace",
      "-Limit", String(body.limit ?? 20)
    ];
    if (body.operation) {
      args.push("-Operation", body.operation);
    }
    return json(res, 200, await runJsonPowerShell(auditScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/crystallize") {
    const body = await readJsonBody(req);
    const args = [
      "-ActionIds", body.actionIds || "",
      "-Project", body.project || "e-github-workspace"
    ];
    if (body.sessionId) {
      args.push("-SessionId", body.sessionId);
    }
    return json(res, 200, await runJsonPowerShell(crystallizeScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/reflect") {
    const body = await readJsonBody(req);
    const args = [
      "-Action", body.action || "insights",
      "-Project", body.project || "e-github-workspace"
    ];
    if (body.query) {
      args.push("-Query", body.query);
    }
    return json(res, 200, await runJsonPowerShell(reflectScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/profile-view") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace"
    ];
    if (body.refresh) {
      args.push("-Refresh");
    }
    return json(res, 200, await runJsonPowerShell(profileViewScript, args));
  }

  if (req.method === "POST" && url.pathname === "/api/obsidian-export") {
    const body = await readJsonBody(req);
    const args = [
      "-Project", body.project || "e-github-workspace"
    ];
    if (body.vaultDir) {
      args.push("-VaultDir", body.vaultDir);
    }
    if (body.types) {
      args.push("-Types", body.types);
    }
    return json(res, 200, await runJsonPowerShell(obsidianExportScript, args));
  }

  json(res, 404, { ok: false, error: "not_found", path: url.pathname });
});

server.listen(port, "127.0.0.1", () => {
  const address = server.address();
  const activePort = typeof address === "object" && address ? address.port : port;
  console.log(JSON.stringify({
    ok: true,
    service: "bhm-workbench",
    url: `http://127.0.0.1:${activePort}`,
    uiPath,
  }));
});
