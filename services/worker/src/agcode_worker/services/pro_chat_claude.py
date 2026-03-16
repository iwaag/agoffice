import asyncio
import os

from agcode_worker.services.chat_transport import ChatTransport

WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT") or os.getcwd()
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_CHUNK_SIZE = 96
CLAUDE_CHUNK_DELAY_SECONDS = 0.015


def _chunk_text(text: str, chunk_size: int = CLAUDE_CHUNK_SIZE) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


async def _send_error(transport: ChatTransport, message: str) -> None:
    await transport.send_json(
        {"type": "error", "provider": "claude", "message": message}
    )


async def run_claude_turn(transport: ChatTransport, prompt: str) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "-p",
            prompt,
            cwd=WORKSPACE_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        await _send_error(transport, f"'{CLAUDE_BIN}' command was not found")
        return None

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_bytes, stderr_bytes = await process.communicate()
    assistant_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    if process.returncode != 0:
        detail = stderr_text or f"claude exited with status {process.returncode}"
        await _send_error(transport, detail)
        return None

    if not assistant_text:
        await _send_error(transport, "claude returned an empty response")
        return None

    for chunk in _chunk_text(assistant_text):
        await transport.send_json(
            {"type": "delta", "provider": "claude", "content": chunk}
        )
        await asyncio.sleep(CLAUDE_CHUNK_DELAY_SECONDS)

    if stderr_text:
        await transport.send_json(
            {"type": "agent_stderr", "provider": "claude", "data": stderr_text}
        )

    await transport.send_json(
        {"type": "assistant_message", "provider": "claude", "content": assistant_text}
    )
    await transport.send_json({"type": "done", "provider": "claude"})
    return assistant_text
