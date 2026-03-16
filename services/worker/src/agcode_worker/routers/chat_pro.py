import logging
import os
from typing import Any
from urllib.parse import parse_qs

import socketio

from agcode_worker.services.pro_chat import ChatSession

socket_server = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
_sessions: dict[str, ChatSession] = {}

AGENT_PROVIDER = os.getenv("AGENT_PROVIDER")
if AGENT_PROVIDER not in {None, "", "CODEX", "CLAUDE"}:
    raise Exception("AGENT_PROVIDER must be CODEX or CLAUDE in pro worker")


class HandshakeLoggingASGIApp(socketio.ASGIApp):
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"}:
            query_string = scope.get("query_string", b"")
            if isinstance(query_string, bytes):
                query = parse_qs(query_string.decode("utf-8", errors="replace"))
            else:
                query = parse_qs(str(query_string))

            if "EIO" in query and "transport" in query:
                logging.info(
                    "socket.io handshake: type=%s path=%s transport=%s eio=%s client=%s",
                    scope.get("type"),
                    scope.get("path"),
                    query.get("transport", ["?"])[0],
                    query.get("EIO", ["?"])[0],
                    scope.get("client"),
                )
            else:
                logging.info(
                    "socket.io handshake: type=%s path=%s client=%s",
                    scope.get("type"),
                    scope.get("path"),
                    scope.get("client"),
                )
        else:
            logging.info("unknown scope: %s", scope)
        await super().__call__(scope, receive, send)



class SocketIOTransport:
    def __init__(self, sid: str) -> None:
        self.sid = sid

    async def send_json(self, payload: dict[str, Any]) -> None:
        await socket_server.emit(payload["type"], payload, to=self.sid)

    async def close(self) -> None:
        await socket_server.disconnect(self.sid)


def _resolve_provider(auth: Any, environ: dict[str, Any]) -> str:
    requested: str | None = None
    if isinstance(auth, dict):
        requested = auth.get("provider")
    if not requested:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        requested = query.get("provider", [None])[0]

    provider = (requested or AGENT_PROVIDER or "CODEX").upper()
    if provider not in {"CODEX", "CLAUDE"}:
        raise ConnectionRefusedError(f"unsupported provider '{provider}'")
    return provider


@socket_server.event
async def connect(sid: str, environ: dict[str, Any], auth: Any | None = None) -> None:
    logging.info("CONNECTING")
    provider = _resolve_provider(auth, environ)
    session = ChatSession(SocketIOTransport(sid), provider)
    _sessions[sid] = session
    await session.start()


@socket_server.event
async def disconnect(sid: str) -> None:
    _sessions.pop(sid, None)


@socket_server.on("ping")
async def ping(sid: str, payload: Any | None = None) -> None:
    session = _sessions.get(sid)
    if session is None:
        return
    await session.handle_message({"type": "ping"})


@socket_server.on("close")
async def close(sid: str, payload: Any | None = None) -> None:
    session = _sessions.get(sid)
    if session is None:
        return
    await session.handle_message({"type": "close"})


FILE_PATH_MAP: dict[str, str] = {
    "auth.json": "~/.codex/auth.json"
    # 例: "config.json": "/path/to/config.json",
}


def _write_mapped_file(name: str, data: bytes) -> None:
    dest = FILE_PATH_MAP.get(name)
    if dest is None:
        return
    dest = os.path.expanduser(dest)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    logging.info("user_file: wrote %s -> %s (%d bytes)", name, dest, len(data))


@socket_server.on("user_file")
async def user_file(_sid: str, payload: Any = None) -> None:
    if isinstance(payload, dict):
        name = payload.get("name", "<unknown>")
        data: bytes = payload.get("data", b"")
        size = payload.get("size", len(data))
    else:
        name = "<unknown>"
        data = payload if isinstance(payload, (bytes, bytearray)) else b""
        size = len(data)
    logging.info("user_file: name=%s size=%d", name, size)
    _write_mapped_file(name, data)


@socket_server.on("user_message")
async def user_message(sid: str, payload: Any = None) -> None:
    logging.info("user_message: %s", payload)
    session = _sessions.get(sid)
    if session is None:
        return

    content = payload
    if isinstance(payload, dict):
        content = payload.get("content", "")

    await session.handle_message({"type": "user_message", "content": content})
