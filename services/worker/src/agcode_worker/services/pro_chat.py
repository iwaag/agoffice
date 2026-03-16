import asyncio
from typing import Any

from agcode_worker.services.chat_transport import ChatTransport
from agcode_worker.services.pro_chat_claude import run_claude_turn
from agcode_worker.services.pro_chat_codex import run_codex_turn


def _build_prompt(transcript: list[dict[str, str]]) -> str:
    lines = [
        "Continue the following conversation.",
        "Reply with the assistant's next message only.",
        "",
    ]
    for item in transcript:
        role = item["role"].upper()
        lines.append(f"{role}:")
        lines.append(item["content"].strip())
        lines.append("")
    return "\n".join(lines)


async def _send_error(transport: ChatTransport, provider: str, message: str) -> None:
    await transport.send_json(
        {"type": "error", "provider": provider.lower(), "message": message}
    )


async def _run_turn(
    transport: ChatTransport, provider: str, transcript: list[dict[str, str]]
) -> str | None:
    await transport.send_json(
        {
            "type": "turn_started",
            "provider": provider.lower(),
            "mode": "stream" if provider == "CODEX" else "pseudo-stream",
        }
    )
    prompt = _build_prompt(transcript)
    if provider == "CODEX":
        return await run_codex_turn(transport, prompt)
    if provider == "CLAUDE":
        return await run_claude_turn(transport, prompt)
    await _send_error(transport, provider.lower(), f"unsupported provider '{provider}'")
    return None


class ChatSession:
    def __init__(self, transport: ChatTransport, provider: str) -> None:
        self.transport = transport
        self.provider = provider
        self.transcript: list[dict[str, str]] = []
        self._turn_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.transport.send_json(
            {
                "type": "ready",
                "provider": self.provider.lower(),
                "accepted_message_types": ["user_message", "ping", "close"],
            }
        )

    async def handle_message(self, payload: dict[str, Any]) -> None:
        message_type = payload.get("type")

        if message_type == "ping":
            await self.transport.send_json(
                {"type": "pong", "provider": self.provider.lower()}
            )
            return

        if message_type == "close":
            await self.transport.close()
            return

        if message_type != "user_message":
            await _send_error(
                self.transport,
                self.provider.lower(),
                "unsupported message type; send 'user_message'",
            )
            return

        raw_content = payload.get("content", "")
        content = "" if raw_content is None else str(raw_content).strip()
        if not content:
            await _send_error(
                self.transport, self.provider.lower(), "content must not be empty"
            )
            return

        async with self._turn_lock:
            self.transcript.append({"role": "user", "content": content})
            assistant_text = await _run_turn(
                self.transport, self.provider, self.transcript
            )
            if assistant_text:
                self.transcript.append({"role": "assistant", "content": assistant_text})
