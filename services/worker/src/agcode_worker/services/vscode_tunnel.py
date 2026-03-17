import asyncio
from datetime import timedelta
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agpyutils.auth import auth_info_from_bearer_token, issue_own_client_access_token
from agpyutils.task import get_task_hub, models as task_models

VSCODE_CLI_BIN = os.getenv("VSCODE_CLI_BIN", "code")

PID_FILE = Path(os.getenv("VSCODE_TUNNEL_PID_FILE", "/tmp/vscode-tunnel.pid"))
LOG_FILE = Path(os.getenv("VSCODE_TUNNEL_LOG_FILE", "/tmp/vscode-tunnel.log"))
TUNNEL_TIMEOUT = int(os.getenv("VSCODE_TUNNEL_TIMEOUT_SECONDS", "30"))
_TUNNEL_URL_PATTERN = re.compile(r"https://vscode\.dev/tunnel/\S+")
_DEVICE_LOGIN_PATTERN = re.compile(
    r"please log into (https://github\.com/login/device) and use code ([A-Z0-9]{4}-[A-Z0-9]{4})"
)

AUTH_TOKEN = os.getenv("AUTH_TOKEN", None)
my_token = asyncio.run(issue_own_client_access_token(AUTH_TOKEN))
my_auth_info = asyncio.run(auth_info_from_bearer_token(my_token))

task_hub = get_task_hub()
@dataclass(frozen=True)
class DeviceLoginPrompt:
    url: str
    code: str


@dataclass(frozen=True)
class TunnelStartResult:
    status: Literal["ok", "already_running", "manual_auth_required"]
    pid: int
    url: str | None = None
    redirect_url: str | None = None
    code: str | None = None


def _extract_device_login(log_content: str) -> DeviceLoginPrompt | None:
    match = _DEVICE_LOGIN_PATTERN.search(log_content)
    if match is None:
        return None
    return DeviceLoginPrompt(url=match.group(1), code=match.group(2))


def _extract_tunnel_url(log_content: str) -> str | None:
    match = _TUNNEL_URL_PATTERN.search(log_content)
    if match is None:
        return None
    return match.group(0)


def _read_log_content() -> str:
    if not LOG_FILE.exists():
        return ""
    return LOG_FILE.read_text(encoding="utf-8", errors="replace")


def _get_running_pid() -> int | None:
    if not PID_FILE.exists():
        return None

    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        PID_FILE.unlink(missing_ok=True)
        return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        return None

    return pid


def _result_for_existing_process(pid: int, log_content: str) -> TunnelStartResult:
    tunnel_url = _extract_tunnel_url(log_content)
    if tunnel_url is not None:
        return TunnelStartResult(status="already_running", pid=pid, url=tunnel_url)

    prompt = _extract_device_login(log_content)
    if prompt is not None:
        return TunnelStartResult(
            status="manual_auth_required",
            pid=pid,
            redirect_url=prompt.url,
            code=prompt.code,
        )

    return TunnelStartResult(status="already_running", pid=pid)


async def start_tunnel(*, tunnel_name: str, host_token: str) -> TunnelStartResult:
    running_pid = _get_running_pid()
    if running_pid is not None:
        return _result_for_existing_process(running_pid, _read_log_content())

    LOG_FILE.write_text("", encoding="utf-8")
    log_fd = open(LOG_FILE, "w", encoding="utf-8")
    env = os.environ.copy()
    env["VSCODE_CLI_ACCESS_TOKEN"] = host_token

    try:
        proc = await asyncio.create_subprocess_exec(
            VSCODE_CLI_BIN,
            "tunnel",
            "--name",
            tunnel_name,
            "--accept-server-license-terms",
            stdout=log_fd,
            stderr=log_fd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"'{VSCODE_CLI_BIN}' command was not found") from exc
    finally:
        log_fd.close()
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    for _ in range(TUNNEL_TIMEOUT):
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        log_content = _read_log_content()
        tunnel_url = _extract_tunnel_url(log_content)
        if tunnel_url is not None:
            return TunnelStartResult(status="ok", pid=proc.pid, url=tunnel_url)

        prompt = _extract_device_login(log_content)
        if prompt is not None:
            task_hub.request_unmanaged_labor(
                task=task_models.Task_UnmanagedLabor(
                    meta=task_models.TaskMetadata(task_id="", user_id=my_auth_info.user_id, project_id=""),
                    redirect_url=prompt.url, wait_for=timedelta(seconds=5)
                )
            )
            return TunnelStartResult(
                status="manual_auth_required",
                pid=proc.pid,
                redirect_url=prompt.url,
                code=prompt.code,
            )

        if proc.returncode is not None:
            PID_FILE.unlink(missing_ok=True)
            logging.error(
                "code tunnel exited with code %s:\n%s", proc.returncode, log_content
            )
            raise RuntimeError(
                f"code tunnel exited unexpectedly (code {proc.returncode})"
            )

    log_content = _read_log_content()
    logging.error("code tunnel timed out after %ss:\n%s", TUNNEL_TIMEOUT, log_content)
    raise TimeoutError(f"tunnel URL not found within {TUNNEL_TIMEOUT}s")
