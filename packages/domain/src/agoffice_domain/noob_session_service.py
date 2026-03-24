from __future__ import annotations

from agoffice_domain.errors import NoobSessionConflictError, NoobThreadNotFoundError, SessionAccessDeniedError, SessionNotFoundError
from agoffice_domain.schema import (
    NoobSessionCreateRequest,
    NoobSessionInfo,
    NoobWorkspacePrepSpec,
    NoobWorkspacePrepRequest,
    NoobThreadCreateRequest,
    NoobThreadInfo,
    NoobThreadRequest,
    NoobTaskRequest,
)


def _to_noob_session_info(model: object) -> NoobSessionInfo:
    return NoobSessionInfo(
        id=getattr(model, "id"),
        user_id=getattr(model, "user_id"),
        project_id=getattr(model, "project_id"),
        title=getattr(model, "title"),
        initial_instruction=getattr(model, "initial_instruction"),
        created_at=getattr(model, "created_at"),
        updated_at=getattr(model, "updated_at"),
        finished_at=getattr(model, "finished_at"),
        config=getattr(model, "config"),
    )


def _to_noob_thread_info(model: object) -> NoobThreadInfo:
    return NoobThreadInfo(
        id=getattr(model, "id"),
        noob_session_id=getattr(model, "noob_session_id"),
        title=getattr(model, "title"),
        keep_context=getattr(model, "keep_context"),
        status=getattr(model, "status"),
        created_at=getattr(model, "created_at"),
        updated_at=getattr(model, "updated_at"),
    )


def create_noob_session(repository: object, *, user_id: str, request: NoobSessionCreateRequest) -> NoobSessionInfo:
    active = repository.get_active_noob_session_for_user(user_id)
    if active is not None:
        raise NoobSessionConflictError(f"User {user_id} already has an active NOOB session: {active.id}")
    return _to_noob_session_info(repository.new_noob_session(user_id=user_id, session_config=request))


def get_owned_noob_session(repository: object, *, session_id: str, user_id: str) -> object:
    session = repository.get_noob_session(session_id)
    if session is None:
        raise SessionNotFoundError(f"NOOB session {session_id} not found")
    if session.user_id != user_id:
        raise SessionAccessDeniedError(f"NOOB session {session_id} access denied")
    return session


def create_or_get_thread(
    repository: object,
    *,
    noob_session_id: str,
    user_id: str,
    request: NoobThreadCreateRequest,
) -> NoobThreadInfo:
    get_owned_noob_session(repository, session_id=noob_session_id, user_id=user_id)
    existing = repository.get_active_noob_thread(noob_session_id)
    if existing is not None:
        return _to_noob_thread_info(existing)
    return _to_noob_thread_info(repository.create_noob_thread(noob_session_id, request))


def get_owned_noob_thread(repository: object, *, noob_session_id: str, thread_id: str, user_id: str) -> object:
    get_owned_noob_session(repository, session_id=noob_session_id, user_id=user_id)
    thread = repository.get_noob_thread(thread_id)
    if thread is None or thread.noob_session_id != noob_session_id:
        raise NoobThreadNotFoundError(f"NOOB thread {thread_id} not found")
    return thread


def build_thread_task_request(thread: object, request: NoobThreadRequest) -> NoobTaskRequest:
    return NoobTaskRequest(
        instruction=request.instruction,
        context_file_paths=request.context_file_paths,
        workspace_path=request.workspace_path or "workspace",
        output_file_path=request.output_file_path,
        system_prompt=request.system_prompt,
        model=request.model,
        thread_id=getattr(thread, "id"),
    )


def resolve_prep_request(
    repository: object,
    *,
    noob_session_id: str,
    user_id: str,
    request: NoobWorkspacePrepRequest | None,
) -> NoobWorkspacePrepRequest:
    session = get_owned_noob_session(repository, session_id=noob_session_id, user_id=user_id)
    if request is not None:
        return request

    config = getattr(session, "config", {}) or {}
    prep = config.get("prep")
    if isinstance(prep, dict):
        return NoobWorkspacePrepRequest(spec=NoobWorkspacePrepSpec.model_validate(prep))
    raise ValueError(f"NOOB session {noob_session_id} does not have a stored prep spec")
