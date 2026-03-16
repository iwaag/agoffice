from fastapi import APIRouter, Depends, HTTPException
from agpyutils.auth import get_auth_info, AuthInfo
from sse_starlette.sse import EventSourceResponse

from agcode_domain import session_service
from agcode_domain.errors import SessionAccessDeniedError, SessionNotFoundError
from agcode_domain.schema import SessionConfig, SessionInfo, SessionListInfo, SessionUpdate
from agcode_infra.db import database as db
from agcode_infra.orchestration import session_k8s as task_session
from agcode_infra.pubsub import redis as redis_service

router = APIRouter()


def _raise_http_session_error(exc: Exception) -> None:
    if isinstance(exc, SessionNotFoundError):
        raise HTTPException(status_code=404, detail="Session not found")
    if isinstance(exc, SessionAccessDeniedError):
        raise HTTPException(status_code=403, detail="Session access denied")
    raise exc


@router.post("/new", summary="New task session")
async def new_session(session: SessionConfig,  auth: AuthInfo = Depends(get_auth_info)) -> SessionInfo:
    return session_service.create_session(
        db,
        user_id=auth.user_id,
        session_config=session,
    )

@router.post("/open", summary="Open task session.")
async def open_session(session_id: str,  auth: AuthInfo = Depends(get_auth_info)) -> SessionInfo:
    try:
        return await session_service.open_session(
            db,
            task_session,
            session_id=session_id,
            user_id=auth.user_id,
        )
    except (SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_session_error(exc)


@router.get("/list", summary="Task session list")
async def task_list(project_id: str, auth: AuthInfo = Depends(get_auth_info)) -> SessionListInfo:
    return session_service.list_sessions(
        db,
        user_id=auth.user_id,
        project_id=project_id,
    )


@router.post("/hook/{session_id}", summary="Webhook to receive session updates from workers")
async def hook_on_update(session_id: str, updates: SessionUpdate) -> SessionInfo:
    return await session_service.apply_session_update(
        db,
        redis_service,
        session_id=session_id,
        updates=updates,
    )


@router.get("/stream/{session_id}", summary="SSE stream for real-time session updates")
async def stream_session(session_id: str, auth: AuthInfo = Depends(get_auth_info)):
    async def event_generator():
        try:
            async for message in session_service.subscribe_session_updates(
                db,
                redis_service,
                session_id=session_id,
                user_id=auth.user_id,
            ):
                yield {"data": message}
        except (SessionNotFoundError, SessionAccessDeniedError) as exc:
            _raise_http_session_error(exc)

    return EventSourceResponse(event_generator())
