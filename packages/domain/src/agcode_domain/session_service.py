from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
from typing import Protocol

from agcode_domain.errors import SessionAccessDeniedError, SessionNotFoundError
from agcode_domain.schema import SessionConfig, SessionInfo, SessionListInfo, SessionUpdate
from agcode_domain.session_mapping import session_model_sequence_to_sceme, session_model_to_scheme


class SessionRecord(Protocol):
    id: str
    user_id: str
    project_id: str


class SessionRepository(Protocol):
    def new_session(self, user_id: str, session_config: SessionConfig) -> SessionRecord: ...
    def update_session(self, session_id: str, updates: SessionUpdate) -> SessionRecord: ...
    def get_session(self, session_id: str) -> SessionRecord | None: ...
    def list_sessions(self, user_id: str, project_id: str) -> Sequence[SessionRecord]: ...


class SessionRuntime(Protocol):
    async def run_session(self, session_id: str, project_id: str, user_id: str) -> SessionInfo: ...
    def get_pro_realtime_socketio_base_url(self, session_id: str) -> str: ...


class SessionEventBus(Protocol):
    def session_channel(self, session_id: str) -> str: ...
    async def publish(self, channel: str, message: str) -> None: ...
    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]: ...


def get_owned_session(
    repository: SessionRepository,
    *,
    session_id: str,
    user_id: str,
) -> SessionRecord:
    session = repository.get_session(session_id)
    if session is None:
        raise SessionNotFoundError(f"Session {session_id} not found")
    if session.user_id != user_id:
        raise SessionAccessDeniedError(f"Session {session_id} access denied")
    return session


def create_session(
    repository: SessionRepository,
    *,
    user_id: str,
    session_config: SessionConfig,
) -> SessionInfo:
    return session_model_to_scheme(repository.new_session(user_id=user_id, session_config=session_config))


async def open_session(
    repository: SessionRepository,
    runtime: SessionRuntime,
    *,
    session_id: str,
    user_id: str,
) -> SessionInfo:
    session = get_owned_session(repository, session_id=session_id, user_id=user_id)
    await runtime.run_session(session_id=session.id, project_id=session.project_id, user_id=user_id)
    updated = repository.update_session(
        session.id,
        SessionUpdate(task_started_at=datetime.now()),
    )
    return session_model_to_scheme(updated)


def list_sessions(
    repository: SessionRepository,
    *,
    user_id: str,
    project_id: str,
) -> SessionListInfo:
    return session_model_sequence_to_sceme(repository.list_sessions(user_id, project_id))


async def apply_session_update(
    repository: SessionRepository,
    event_bus: SessionEventBus,
    *,
    session_id: str,
    updates: SessionUpdate,
) -> SessionInfo:
    updated = repository.update_session(session_id, updates)
    session_info = session_model_to_scheme(updated)
    await event_bus.publish(
        event_bus.session_channel(session_id),
        session_info.model_dump_json(),
    )
    return session_info


def get_owned_realtime_base_url(
    repository: SessionRepository,
    runtime: SessionRuntime,
    *,
    session_id: str,
    user_id: str,
) -> str:
    get_owned_session(repository, session_id=session_id, user_id=user_id)
    return runtime.get_pro_realtime_socketio_base_url(session_id)


async def subscribe_session_updates(
    repository: SessionRepository,
    event_bus: SessionEventBus,
    *,
    session_id: str,
    user_id: str,
) -> AsyncGenerator[str, None]:
    get_owned_session(repository, session_id=session_id, user_id=user_id)
    async for message in event_bus.subscribe(event_bus.session_channel(session_id)):
        yield message
