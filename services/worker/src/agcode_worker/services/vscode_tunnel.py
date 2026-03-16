import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

VSCODE_CLI_BIN = os.getenv("VSCODE_CLI_BIN", "code")
PID_FILE = Path(os.getenv("VSCODE_TUNNEL_PID_FILE", "/tmp/vscode-tunnel.pid"))
LOG_FILE = Path(os.getenv("VSCODE_TUNNEL_LOG_FILE", "/tmp/vscode-tunnel.log"))
TUNNEL_TIMEOUT = int(os.getenv("VSCODE_TUNNEL_TIMEOUT_SECONDS", "30"))
_TUNNEL_URL_PATTERN = re.compile(r"https://vscode\.dev/tunnel/\S+")


@dataclass(frozen=True)
class TunnelStartResult:
    status: str
    pid: int
    url: str | None = None


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


async def start_tunnel(*, tunnel_name: str, host_token: str) -> TunnelStartResult:
    running_pid = _get_running_pid()
    if running_pid is not None:
        return TunnelStartResult(
            status="already_running",
            pid=running_pid,
            url=_extract_tunnel_url(_read_log_content()),
        )

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
