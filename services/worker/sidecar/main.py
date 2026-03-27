import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from agpy.task.hub import TaskHub
from agpy.task import get_task_hub


hub = get_task_hub()
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
WORKSPACE = "/workspace"
PID_FILE = Path("/tmp/vscode-tunnel.pid")
LOG_FILE = Path("/tmp/vscode-tunnel.log")
TUNNEL_TIMEOUT = 30


# --- Response models ---

class OkOutputResponse(BaseModel):
    status: Literal["ok"]
    output: str

class OkGitHostResponse(BaseModel):
    status: Literal["ok"]
    git_host: str

class TunnelStartedResponse(BaseModel):
    status: Literal["ok"]
    tunnel_name: str

class TunnelAlreadyRunningResponse(BaseModel):
    status: Literal["already_running"]
    pid: int
    tunnel_name: str


# --- Request models ---

class StartTunnelRequest(BaseModel):
    tunnel_name: str
    host_token: str

class GitAuthRequest(BaseModel):
    github_token: str
    git_host: str = "github.com"

# --- Handlers ---

@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    logging.error(f"caught exception: {exc.detail}", exc_info=True)
    return exc

@app.post("/on-push", response_model=OkOutputResponse)
async def on_push() -> OkOutputResponse:
    proc = await asyncio.create_subprocess_exec(
        "git", "pull",
        cwd=WORKSPACE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=stderr.decode())
    return OkOutputResponse(status="ok", output=stdout.decode())


@app.post("/start-tunnel", response_model=TunnelStartedResponse | TunnelAlreadyRunningResponse)
async def start_tunnel(req: StartTunnelRequest) -> TunnelStartedResponse | TunnelAlreadyRunningResponse:
    # Prevent duplicate tunnel
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            return TunnelAlreadyRunningResponse(
                status="already_running",
                pid=pid,
                tunnel_name=req.tunnel_name,
            )
        except ProcessLookupError:
            PID_FILE.unlink(missing_ok=True)

    # Start tunnel in background
    LOG_FILE.write_text("")
    log_fd = open(LOG_FILE, "w")
    env = os.environ.copy()
    env["VSCODE_CLI_ACCESS_TOKEN"] = req.host_token
    proc = await asyncio.create_subprocess_exec(
        # "--host-token", req.host_token,  # env var method used instead
        "code", "tunnel", "--name", req.tunnel_name, "--accept-server-license-terms",
        stdout=log_fd,
        stderr=log_fd,
        env=env,
    )
    log_fd.close()
    PID_FILE.write_text(str(proc.pid))

    # Wait for tunnel URL
    for _ in range(TUNNEL_TIMEOUT):
        await asyncio.sleep(1)
        log_content = LOG_FILE.read_text()
        match = re.search(r"https://vscode\.dev/tunnel/\S+", log_content)
        if match:
            return TunnelStartedResponse(status="ok", tunnel_name=req.tunnel_name)
        if proc.returncode is not None:
            logging.error(f"code tunnel exited with code {proc.returncode}:\n{log_content}")
            raise HTTPException(status_code=500, detail=f"code tunnel exited unexpectedly (code {proc.returncode})")

    log_content = LOG_FILE.read_text()
    logging.error(f"code tunnel timed out after {TUNNEL_TIMEOUT}s:\n{log_content}")
    raise HTTPException(status_code=504, detail=f"tunnel URL not found within {TUNNEL_TIMEOUT}s")


@app.post("/setup-git-auth", response_model=OkGitHostResponse)
async def setup_git_auth(req: GitAuthRequest) -> OkGitHostResponse:
    # Configure credential helper
    cfg = await asyncio.create_subprocess_exec(
        "git", "config", "--global", "credential.helper", "store",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await cfg.communicate()
    if cfg.returncode != 0:
        raise HTTPException(status_code=500, detail=stderr.decode())

    creds_path = Path.home() / ".git-credentials"
    creds_path.write_text(f"https://x-token-auth:{req.github_token}@{req.git_host}\n")
    creds_path.chmod(0o600)
    return OkGitHostResponse(status="ok", git_host=req.git_host)


@app.post("/sync", response_model=OkOutputResponse)
async def sync() -> OkOutputResponse:
    fetch = await asyncio.create_subprocess_exec(
        "git", "fetch", "--all",
        cwd=WORKSPACE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await fetch.communicate()

    reset = await asyncio.create_subprocess_exec(
        "git", "reset", "--hard", "origin/main",
        cwd=WORKSPACE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await reset.communicate()
    if reset.returncode != 0:
        raise HTTPException(status_code=500, detail=stderr.decode())
    return OkOutputResponse(status="ok", output=stdout.decode())
