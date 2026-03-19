import { appendFile, mkdir, readFile, readdir, rename, stat, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";

const sessionRoot = process.env.NOOB_SESSION_ROOT || "/mnt/session";
const pollIntervalMs = Number(process.env.NOOB_POLL_INTERVAL_MS || "1000");
const heartbeatIntervalMs = Number(process.env.NOOB_HEARTBEAT_INTERVAL_MS || "5000");
const agentCommand = process.env.NOOB_AGENT_COMMAND || "pi-coding-agent";

const controlDir = path.join(sessionRoot, "control");
const eventsDir = path.join(sessionRoot, "events");
const stateDir = path.join(sessionRoot, "state");
const artifactsDir = path.join(sessionRoot, "artifacts");

function nowIso() {
  return new Date().toISOString();
}

async function ensureLayout() {
  for (const dir of [controlDir, eventsDir, stateDir, artifactsDir]) {
    await mkdir(dir, { recursive: true });
  }
}

async function writeJsonAtomic(targetPath, payload) {
  const tempPath = `${targetPath}.tmp-${process.pid}`;
  await writeFile(tempPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  await rename(tempPath, targetPath);
}

async function appendEvent(payload) {
  const requestId = payload.request_id || "session";
  const eventPath = path.join(eventsDir, `event-${requestId}.jsonl`);
  await appendFile(eventPath, `${JSON.stringify({ timestamp: nowIso(), ...payload })}\n`, "utf8");
}

async function updateStatus(status, extra = {}) {
  await writeJsonAtomic(path.join(stateDir, "status.json"), {
    status,
    updated_at: nowIso(),
    ...extra,
  });
}

async function updateHeartbeat() {
  await writeJsonAtomic(path.join(stateDir, "heartbeat.json"), {
    pid: process.pid,
    updated_at: nowIso(),
  });
}

async function listPendingRequests() {
  const entries = await readdir(controlDir);
  return entries
    .filter((name) => name.startsWith("request-") && name.endsWith(".json"))
    .sort();
}

async function claimRequestFile(fileName) {
  const sourcePath = path.join(controlDir, fileName);
  const claimedName = `${fileName}.working-${process.pid}`;
  const claimedPath = path.join(controlDir, claimedName);
  await rename(sourcePath, claimedPath);
  return claimedPath;
}

async function markRequestDone(claimedPath) {
  await rename(claimedPath, `${claimedPath}.done`);
}

function buildAgentInvocation(request) {
  if (Array.isArray(request.argv) && request.argv.length > 0) {
    return { command: request.argv[0], args: request.argv.slice(1) };
  }

  if (typeof request.command === "string" && request.command.length > 0) {
    const args = Array.isArray(request.args) ? request.args.filter((item) => typeof item === "string") : [];
    return { command: request.command, args };
  }

  const prompt = typeof request.user_prompt === "string" ? request.user_prompt : "";
  if (prompt.length === 0) {
    throw new Error("request must include argv, command, or user_prompt");
  }
  return { command: agentCommand, args: [prompt] };
}

async function resolveWorkingDirectory(request) {
  const requested = typeof request.workspace_path === "string" && request.workspace_path.length > 0
    ? request.workspace_path
    : sessionRoot;
  const candidate = path.isAbsolute(requested) ? requested : path.join(sessionRoot, requested);
  const info = await stat(candidate);
  if (!info.isDirectory()) {
    throw new Error(`workspace_path is not a directory: ${candidate}`);
  }
  return candidate;
}

async function runRequest(request, claimedPath) {
  const requestId = request.request_id || path.basename(claimedPath, path.extname(claimedPath));
  const stdoutPath = path.join(artifactsDir, `${requestId}.stdout.log`);
  const stderrPath = path.join(artifactsDir, `${requestId}.stderr.log`);
  const resultPath = path.join(stateDir, `result-${requestId}.json`);
  const cwd = await resolveWorkingDirectory(request);
  const invocation = buildAgentInvocation(request);

  await appendEvent({ type: "accepted", request_id: requestId, session_id: process.env.TASK_ID || null });
  await updateStatus("running", { request_id: requestId });
  await appendEvent({
    type: "started",
    request_id: requestId,
    payload: { command: invocation.command, args: invocation.args, cwd },
  });

  await writeFile(stdoutPath, "", "utf8");
  await writeFile(stderrPath, "", "utf8");

  const child = spawn(invocation.command, invocation.args, {
    cwd,
    env: process.env,
    stdio: ["pipe", "pipe", "pipe"],
  });

  if (typeof request.stdin === "string" && request.stdin.length > 0) {
    child.stdin.write(request.stdin);
  }
  child.stdin.end();

  child.stdout.on("data", async (chunk) => {
    const text = chunk.toString("utf8");
    await appendFile(stdoutPath, text, "utf8");
    await appendEvent({ type: "stdout", request_id: requestId, payload: { text } });
  });

  child.stderr.on("data", async (chunk) => {
    const text = chunk.toString("utf8");
    await appendFile(stderrPath, text, "utf8");
    await appendEvent({ type: "stderr", request_id: requestId, payload: { text } });
  });

  const exitCode = await new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", resolve);
  });

  const resultPayload = {
    request_id: requestId,
    exit_code: exitCode,
    completed_at: nowIso(),
    stdout_path: stdoutPath,
    stderr_path: stderrPath,
  };
  await writeJsonAtomic(resultPath, resultPayload);

  if (exitCode === 0) {
    await appendEvent({ type: "completed", request_id: requestId, payload: resultPayload });
    await updateStatus("idle", { request_id: requestId });
    return;
  }

  await appendEvent({ type: "failed", request_id: requestId, payload: resultPayload });
  await updateStatus("failed", { request_id: requestId, exit_code: exitCode });
}

async function pollOnce() {
  const pending = await listPendingRequests();
  if (pending.length === 0) {
    return;
  }

  const fileName = pending[0];
  let claimedPath;
  try {
    claimedPath = await claimRequestFile(fileName);
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return;
    }
    throw error;
  }

  const raw = await readFile(claimedPath, "utf8");
  const request = JSON.parse(raw);

  try {
    await runRequest(request, claimedPath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await appendEvent({
      type: "failed",
      request_id: request.request_id || fileName,
      payload: { error: message },
    });
    await updateStatus("failed", { request_id: request.request_id || fileName, error: message });
  } finally {
    await markRequestDone(claimedPath);
  }
}

async function main() {
  await ensureLayout();
  await updateStatus("idle");
  await updateHeartbeat();
  setInterval(() => {
    updateHeartbeat().catch((error) => {
      console.error("heartbeat update failed", error);
    });
  }, heartbeatIntervalMs);

  for (;;) {
    await pollOnce();
    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
}

main().catch(async (error) => {
  const message = error instanceof Error ? error.message : String(error);
  console.error(message);
  try {
    await updateStatus("failed", { error: message });
  } catch (statusError) {
    console.error("failed to write status", statusError);
  }
  process.exitCode = 1;
});
