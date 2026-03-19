import { appendFile, mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
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
const requestPath = path.join(controlDir, "request.json");
const requestProcessingPath = path.join(controlDir, "request.processing.json");
const eventsPath = path.join(eventsDir, "events.jsonl");
const resultPath = path.join(stateDir, "result.json");

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
  await appendFile(eventsPath, `${JSON.stringify({ timestamp: nowIso(), ...payload })}\n`, "utf8");
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

async function claimRequestFile() {
  await rename(requestPath, requestProcessingPath);
  return requestProcessingPath;
}

function buildAgentInvocation(request) {
  if (Array.isArray(request.argv) && request.argv.length > 0) {
    return { command: request.argv[0], args: request.argv.slice(1) };
  }

  if (typeof request.command === "string" && request.command.length > 0) {
    const args = Array.isArray(request.args) ? request.args.filter((item) => typeof item === "string") : [];
    return { command: request.command, args };
  }

  const prompt = buildPrompt(request);
  if (prompt.length === 0) {
    throw new Error("request must include instruction, argv, or command");
  }
  return { command: agentCommand, args: [prompt] };
}

function buildPrompt(request) {
  const parts = [];
  if (typeof request.system_prompt === "string" && request.system_prompt.length > 0) {
    parts.push(request.system_prompt);
  }
  parts.push("Work inside the mounted workspace and write the final response to the requested output file.");
  parts.push(`Instruction:\n${request.instruction || ""}`);
  if (Array.isArray(request.context_file_paths) && request.context_file_paths.length > 0) {
    parts.push(`Reference files to read:\n${request.context_file_paths.join("\n")}`);
  }
  if (typeof request.output_file_path === "string" && request.output_file_path.length > 0) {
    parts.push(`Write the final deliverable to:\n${request.output_file_path}`);
  }
  return parts.join("\n\n").trim();
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

async function runRequest(request) {
  const stdoutPath = path.join(artifactsDir, "stdout.log");
  const stderrPath = path.join(artifactsDir, "stderr.log");
  const cwd = await resolveWorkingDirectory(request);
  const invocation = buildAgentInvocation(request);
  const outputPath = path.isAbsolute(request.output_file_path || "")
    ? request.output_file_path
    : path.join(sessionRoot, request.output_file_path || "artifacts/response.md");
  await mkdir(path.dirname(outputPath), { recursive: true });

  await writeFile(eventsPath, "", "utf8");
  await rm(resultPath, { force: true });
  await appendEvent({
    type: "accepted",
    session_id: process.env.TASK_ID || null,
    payload: { thread_id: request.thread_id || null },
  });
  await updateStatus("running");
  await appendEvent({
    type: "started",
    payload: { command: invocation.command, args: invocation.args, cwd, output_path: outputPath },
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
    await appendEvent({ type: "stdout", payload: { text } });
  });

  child.stderr.on("data", async (chunk) => {
    const text = chunk.toString("utf8");
    await appendFile(stderrPath, text, "utf8");
    await appendEvent({ type: "stderr", payload: { text } });
  });

  const exitCode = await new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", resolve);
  });

  const resultPayload = {
    exit_code: exitCode,
    completed_at: nowIso(),
    output_path: outputPath,
    stdout_path: stdoutPath,
    stderr_path: stderrPath,
    thread_id: request.thread_id || null,
  };
  try {
    resultPayload.content = await readFile(outputPath, "utf8");
  } catch {
    resultPayload.content = null;
  }
  await writeJsonAtomic(resultPath, resultPayload);

  if (exitCode === 0) {
    await appendEvent({ type: "completed", payload: resultPayload });
    await updateStatus("succeeded", { exit_code: 0 });
    return;
  }

  await appendEvent({ type: "failed", payload: resultPayload });
  await updateStatus("failed", { exit_code: exitCode });
}

async function pollOnce() {
  let claimedPath;
  try {
    claimedPath = await claimRequestFile();
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return;
    }
    throw error;
  }

  const raw = await readFile(claimedPath, "utf8");
  const request = JSON.parse(raw);

  try {
    await runRequest(request);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await appendEvent({
      type: "failed",
      payload: { error: message, thread_id: request.thread_id || null },
    });
    await writeJsonAtomic(resultPath, {
      error: message,
      completed_at: nowIso(),
      thread_id: request.thread_id || null,
    });
    await updateStatus("failed", { error: message });
  } finally {
    await rm(claimedPath, { force: true });
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
