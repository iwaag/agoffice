import asyncio
import os
from dataclasses import dataclass
from urllib.parse import parse_qs

import socketio
from agpyutils.auth import get_auth_info
from fastapi.security import HTTPAuthorizationCredentials

from agcode_domain import session_service
from agcode_domain.errors import SessionAccessDeniedError, SessionNotFoundError
from agcode_infra.db import database as db
from agcode_infra.orchestration import session_k8s as task_session

SOCKETIO_PATH = os.getenv("SESSION_SOCKETIO_PATH", "/session/socket.io")
UPSTREAM_SOCKETIO_PATH = task_session.WORKER_SOCKETIO_PATH
PROXY_NAMESPACE = os.getenv("SESSION_SOCKETIO_NAMESPACE", "/")
UPSTREAM_NAMESPACE = os.getenv("SESSION_WORKER_SOCKETIO_NAMESPACE", "/")


def _normalize_namespace(value: str) -> str:
    if not value or value == "/":
        return "/"
    return value if value.startswith("/") else f"/{value}"


PROXY_NAMESPACE = _normalize_namespace(PROXY_NAMESPACE)
UPSTREAM_NAMESPACE = _normalize_namespace(UPSTREAM_NAMESPACE)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
)


@dataclass
class _BridgeConnection:
    upstream: socketio.AsyncClient


class _UpstreamNamespace(socketio.AsyncClientNamespace):
    def __init__(self, namespace: str, proxy_namespace: "_ProxyNamespace", sid: str):
        super().__init__(namespace)
        self._proxy_namespace = proxy_namespace
        self._sid = sid

    async def trigger_event(self, event, *args):
        if event in {"connect", "disconnect"}:
            return await super().trigger_event(event, *args)

        payload = args[0] if len(args) == 1 else list(args)
        await self._proxy_namespace.emit(event, payload, to=self._sid)

    async def on_disconnect(self):
        await self._proxy_namespace.disconnect(self._sid)


class _ProxyNamespace(socketio.AsyncNamespace):
    def __init__(self, namespace: str):
        super().__init__(namespace)
        self._bridges: dict[str, _BridgeConnection] = {}
        self._lock = asyncio.Lock()

    async def on_connect(self, sid, environ, auth):
        session_id = self._extract_session_id(environ=environ, auth=auth)
        token = self._extract_token(environ=environ, auth=auth)
        if not token:
            raise ConnectionRefusedError("missing_bearer_token")

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        auth_info = await get_auth_info(credentials=credentials)

        try:
            upstream_base_url = session_service.get_owned_realtime_base_url(
                db,
                task_session,
                session_id=session_id,
                user_id=auth_info.user_id,
            )
        except SessionNotFoundError as exc:
            raise ConnectionRefusedError("session_not_found") from exc
        except SessionAccessDeniedError as exc:
            raise ConnectionRefusedError("session_access_denied") from exc

        upstream = socketio.AsyncClient(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )
        upstream.register_namespace(_UpstreamNamespace(UPSTREAM_NAMESPACE, self, sid))
        try:
            await upstream.connect(
                upstream_base_url,
                socketio_path=UPSTREAM_SOCKETIO_PATH.lstrip("/"),
                namespaces=[UPSTREAM_NAMESPACE],
                headers={"Authorization": f"Bearer {token}"},
                auth={"token": token},
                transports=["websocket"],
                wait_timeout=10,
            )
        except Exception as exc:
            raise ConnectionRefusedError("upstream_connect_failed") from exc

        async with self._lock:
            self._bridges[sid] = _BridgeConnection(upstream=upstream)

    async def on_disconnect(self, sid):
        async with self._lock:
            bridge = self._bridges.pop(sid, None)
        if bridge is not None:
            await bridge.upstream.disconnect()

    async def trigger_event(self, event, sid, *args):
        if event in {"connect", "disconnect"}:
            return await super().trigger_event(event, sid, *args)

        async with self._lock:
            bridge = self._bridges.get(sid)
        if bridge is None:
            return

        payload = args[0] if len(args) == 1 else list(args)
        await bridge.upstream.emit(event, payload, namespace=UPSTREAM_NAMESPACE)

    @staticmethod
    def _extract_session_id(environ: dict, auth: dict | None) -> str:
        if isinstance(auth, dict):
            value = auth.get("session_id")
            if isinstance(value, str) and value:
                return value

        query = parse_qs(environ.get("QUERY_STRING", ""))
        values = query.get("session_id", [])
        if values and values[0]:
            return values[0]

        raise ConnectionRefusedError("missing_session_id")

    @staticmethod
    def _extract_token(environ: dict, auth: dict | None) -> str | None:
        if isinstance(auth, dict):
            for key in ("token", "access_token", "bearer"):
                value = auth.get(key)
                if isinstance(value, str) and value:
                    return value

            authorization = auth.get("authorization")
            if isinstance(authorization, str):
                scheme, _, token = authorization.partition(" ")
                if scheme.lower() == "bearer" and token:
                    return token

        authorization_header = environ.get("HTTP_AUTHORIZATION", "").strip()
        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token

        query = parse_qs(environ.get("QUERY_STRING", ""))
        for key in ("token", "access_token"):
            values = query.get(key, [])
            if values and values[0]:
                return values[0]
        return None


proxy_namespace = _ProxyNamespace(PROXY_NAMESPACE)
sio.register_namespace(proxy_namespace)
