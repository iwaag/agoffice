import asyncio
from datetime import timedelta
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agpy.auth import auth_info_from_bearer_token
from agpy.task import get_task_hub, models as task_models
from pydantic import BaseModel

VSCODE_CLI_BIN = os.getenv("VSCODE_CLI_BIN", "code")

PID_FILE = Path(os.getenv("VSCODE_TUNNEL_PID_FILE", "/tmp/vscode-tunnel.pid"))
LOG_FILE = Path(os.getenv("VSCODE_TUNNEL_LOG_FILE", "/tmp/vscode-tunnel.log"))
TUNNEL_TIMEOUT = int(os.getenv("VSCODE_TUNNEL_TIMEOUT_SECONDS", "30"))
_DEVICE_LOGIN_PATTERN = re.compile(
    r"please log into (https://github\.com/login/device) and use code ([A-Z0-9]{4}-[A-Z0-9]{4})"
)

AUTH_TOKEN = os.getenv("AUTH_TOKEN", None)

task_hub = get_task_hub()


@dataclass(frozen=True)
class DeviceLoginPrompt:
    url: str
    code: str


class TunnelStartResult(BaseModel):
    status: Literal["ok", "already_running", "manual_auth_required"]
    tunnel_name: str


def _extract_device_login(log_content: str) -> DeviceLoginPrompt | None:
    matches = list(_DEVICE_LOGIN_PATTERN.finditer(log_content))
    if not matches:
        return None
    match = matches[-1]
    return DeviceLoginPrompt(url=match.group(1), code=match.group(2))


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


def _check_log_for_result(
    log_content: str,
    tunnel_name: str,
    success_status: Literal["ok", "already_running"],
) -> TunnelStartResult | None:
    if "Open this link in your browser" in log_content or "https://vscode.dev/tunnel/" in log_content:
        return TunnelStartResult(status=success_status, tunnel_name=tunnel_name)
    prompt = _extract_device_login(log_content)
    if prompt is not None:
        return TunnelStartResult(status="manual_auth_required", tunnel_name=tunnel_name)
    return None


async def start_tunnel(*, tunnel_name: str, host_token: str) -> TunnelStartResult:
    running_pid = _get_running_pid()
    if running_pid is not None:
        result = _check_log_for_result(_read_log_content(), tunnel_name, "already_running")
        if result is not None:
            return result
        return TunnelStartResult(status="already_running", tunnel_name=tunnel_name)

    LOG_FILE.write_text("", encoding="utf-8")
    log_fd = open(LOG_FILE, "w", encoding="utf-8")
    #env = os.environ.copy()
    #env["VSCODE_CLI_ACCESS_TOKEN"] = host_token

    try:
        proc = await asyncio.create_subprocess_exec(
            VSCODE_CLI_BIN,
            "tunnel",
            "--name",
            tunnel_name,
            "--accept-server-license-terms",
            stdout=log_fd,
            stderr=log_fd
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"'{VSCODE_CLI_BIN}' command was not found") from exc
    finally:
        log_fd.close()
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    my_auth_info = await auth_info_from_bearer_token(AUTH_TOKEN)
    for _ in range(TUNNEL_TIMEOUT):
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        log_content = _read_log_content()
        result = _check_log_for_result(log_content, tunnel_name, "ok")
        if result is not None:
            if result.status == "manual_auth_required":
                prompt = _extract_device_login(log_content)
                if prompt is None:
                    raise RuntimeError("manual auth required without prompt details")
                task_hub.request_labor_auth(
                    task=task_models.Task_UnmanagedLabor(
                        meta=task_models.TaskMetadata(type_id="code_auth", user_id=my_auth_info.user_id, project_id=""),
                        redirect_url=prompt.url, wait_for=timedelta(minutes=5), hints={"code": prompt.code}
                    )
                )
            return result

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
    raise TimeoutError(f"tunnel start was not confirmed within {TUNNEL_TIMEOUT}s")
