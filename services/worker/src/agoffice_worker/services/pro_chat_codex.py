import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import suppress

from agoffice_worker.services.chat_transport import ChatTransport

WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT") or os.getcwd()
CODEX_BIN = os.getenv("CODEX_BIN", "codex")


async def _iter_stream(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            yield text


async def _read_stream(stream: asyncio.StreamReader) -> str:
    chunks: list[str] = []
    async for text in _iter_stream(stream):
        chunks.append(text)
    return "\n".join(chunks)


async def _send_error(transport: ChatTransport, message: str) -> None:
    await transport.send_json(
        {"type": "error", "provider": "codex", "message": message}
    )


async def run_codex_turn(transport: ChatTransport, prompt: str) -> str | None:
    fd, output_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
    os.close(fd)

    try:
        process = await asyncio.create_subprocess_exec(
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            "-C",
            WORKSPACE_ROOT,
            "--output-last-message",
            output_path,
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        os.unlink(output_path)
        await _send_error(transport, f"'{CODEX_BIN}' command was not found")
        return None

    async def forward_stdout() -> None:
        assert process.stdout is not None
        async for line in _iter_stream(process.stdout):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                await transport.send_json(
                    {"type": "agent_stdout", "provider": "codex", "data": line}
                )
                continue
            await transport.send_json(
                {"type": "agent_event", "provider": "codex", "event": event}
            )

    stdout_task = asyncio.create_task(forward_stdout())
    assert process.stderr is not None
    stderr_text = await _read_stream(process.stderr)
    return_code = await process.wait()
    await stdout_task

    assistant_text = ""
    try:
        with open(output_path, encoding="utf-8") as file:
            assistant_text = file.read().strip()
    except FileNotFoundError:
        pass
    finally:
        with suppress(FileNotFoundError):
            os.unlink(output_path)

    if return_code != 0:
        detail = stderr_text or f"codex exited with status {return_code}"
        await _send_error(transport, detail)
        return None

    if stderr_text:
        await transport.send_json(
            {"type": "agent_stderr", "provider": "codex", "data": stderr_text}
        )

    await transport.send_json(
        {"type": "assistant_message", "provider": "codex", "content": assistant_text}
    )
    await transport.send_json({"type": "done", "provider": "codex"})
    return assistant_text
