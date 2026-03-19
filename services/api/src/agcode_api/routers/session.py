from fastapi import APIRouter, Depends, HTTPException
from agpyutils.auth import get_auth_info, AuthInfo
import httpx
from sse_starlette.sse import EventSourceResponse

from agcode_domain import noob_session_service, session_service
from agcode_domain.errors import (
    NoobSessionConflictError,
    NoobThreadNotFoundError,
    SessionAccessDeniedError,
    SessionNotFoundError,
)
from agcode_domain.schema import (
    NoobSessionCreateRequest,
    NoobSessionInfo,
    NoobTaskAcceptedResponse,
    NoobTaskEvents,
    NoobTaskRequest,
    NoobTaskResult,
    NoobTaskStatus,
    NoobThreadCreateRequest,
    NoobThreadInfo,
    NoobThreadRequest,
    NoobWorkspacePrepStatus,
    SessionConfig,
    SessionInfo,
    SessionListInfo,
    SessionUpdate,
    TunnelInfo,
)
from agcode_infra.db import database as db
from agcode_infra.orchestration import session_k8s as task_session
from agcode_infra.pubsub import redis as redis_service

router = APIRouter()


def _raise_http_session_error(exc: Exception) -> None:
    if isinstance(exc, SessionNotFoundError):
        raise HTTPException(status_code=404, detail="Session not found")
    if isinstance(exc, SessionAccessDeniedError):
        raise HTTPException(status_code=403, detail="Session access denied")
    if isinstance(exc, NoobThreadNotFoundError):
        raise HTTPException(status_code=404, detail="NOOB thread not found")
    if isinstance(exc, NoobSessionConflictError):
        raise HTTPException(status_code=409, detail=str(exc))
    raise exc


def _get_owned_noob_session_like(session_id: str, user_id: str) -> object:
    try:
        return noob_session_service.get_owned_noob_session(
            db,
            session_id=session_id,
            user_id=user_id,
        )
    except SessionNotFoundError:
        return session_service.get_owned_session(
            db,
            session_id=session_id,
            user_id=user_id,
        )


@router.post("/new", summary="New task session")
async def new_session(session: SessionConfig,  auth: AuthInfo = Depends(get_auth_info)) -> SessionInfo:
    return session_service.create_session(
        db,
        user_id=auth.user_id,
        session_config=session,
    )


@router.post("/noob/session/new", summary="Create a NOOB worker session")
async def new_noob_session(
    request: NoobSessionCreateRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobSessionInfo:
    try:
        return noob_session_service.create_noob_session(
            db,
            user_id=auth.user_id,
            request=request,
        )
    except (SessionNotFoundError, SessionAccessDeniedError, NoobSessionConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_session_error(exc)


@router.post("/noob/session/{session_id}/open", summary="Open a NOOB worker session")
async def open_noob_session(session_id: str, auth: AuthInfo = Depends(get_auth_info)) -> NoobSessionInfo:
    try:
        session = noob_session_service.get_owned_noob_session(
            db,
            session_id=session_id,
            user_id=auth.user_id,
        )
        await task_session.run_session(
            session_id=session.id,
            project_id=session.project_id,
            user_id=auth.user_id,
            token=auth.token,
        )
        return NoobSessionInfo(
            id=session.id,
            user_id=session.user_id,
            project_id=session.project_id,
            title=session.title,
            initial_instruction=session.initial_instruction,
            created_at=session.created_at,
            updated_at=session.updated_at,
            finished_at=session.finished_at,
            config=session.config,
        )
    except (SessionNotFoundError, SessionAccessDeniedError, NoobSessionConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_session_error(exc)


@router.post("/noob/session/{session_id}/thread", summary="Create or reuse the active NOOB chat thread")
async def create_noob_thread(
    session_id: str,
    request: NoobThreadCreateRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobThreadInfo:
    try:
        return noob_session_service.create_or_get_thread(
            db,
            noob_session_id=session_id,
            user_id=auth.user_id,
            request=request,
        )
    except (SessionNotFoundError, SessionAccessDeniedError, NoobSessionConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_session_error(exc)


@router.post("/noob/session/{session_id}/thread/{thread_id}/request", summary="Submit a NOOB request scoped to a chat thread")
async def submit_noob_thread_request(
    session_id: str,
    thread_id: str,
    request: NoobThreadRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskAcceptedResponse:
    try:
        thread = noob_session_service.get_owned_noob_thread(
            db,
            noob_session_id=session_id,
            thread_id=thread_id,
            user_id=auth.user_id,
        )
        task_request = noob_session_service.build_thread_task_request(thread, request)
        await task_session.submit_noob_task(
            session_id=session_id,
            user_id=auth.user_id,
            token=auth.token,
            request=task_request,
        )
        db.update_noob_thread_status(thread_id, "running")
        return NoobTaskAcceptedResponse(status="accepted")
    except (SessionNotFoundError, SessionAccessDeniedError, NoobSessionConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_session_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/noob/session/{session_id}/workspace/status", summary="Get NOOB workspace preparation status")
async def get_noob_workspace_status(
    session_id: str,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobWorkspacePrepStatus:
    try:
        noob_session_service.get_owned_noob_session(
            db,
            session_id=session_id,
            user_id=auth.user_id,
        )
        return await task_session.get_noob_workspace_status(session_id)
    except (SessionNotFoundError, SessionAccessDeniedError, NoobSessionConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_session_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.post("/open", summary="Open task session.")
async def open_session(session_id: str,  auth: AuthInfo = Depends(get_auth_info)) -> SessionInfo:
    try:
        return await session_service.open_session(
            db,
            task_session,
            session_id=session_id,
            user_id=auth.user_id,
            token=auth.token,
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


@router.post("/{session_id}/tunnel/start", summary="Start VS Code tunnel for a session")
async def start_session_tunnel(session_id: str, auth: AuthInfo = Depends(get_auth_info)) -> TunnelInfo:
    try:
        return await session_service.start_session_tunnel(
            db,
            task_session,
            session_id=session_id,
            user_id=auth.user_id,
            token=auth.token,
        )
    except (SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_session_error(exc)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Tunnel worker request timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Tunnel worker request failed: {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Tunnel worker is unreachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{session_id}/noob/request", summary="Submit a NOOB worker request")
async def submit_noob_request(
    session_id: str,
    request: NoobTaskRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskAcceptedResponse:
    try:
        session = _get_owned_noob_session_like(session_id, auth.user_id)
        await task_session.submit_noob_task(
            session_id=session.id,
            user_id=auth.user_id,
            token=auth.token,
            request=request,
        )
        return NoobTaskAcceptedResponse(status="accepted")
    except (SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_session_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}/noob/status", summary="Get NOOB worker status")
async def get_noob_status(session_id: str, auth: AuthInfo = Depends(get_auth_info)) -> NoobTaskStatus:
    try:
        session = _get_owned_noob_session_like(session_id, auth.user_id)
        return await task_session.get_noob_task_status(session.id)
    except (SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_session_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}/noob/result", summary="Get NOOB worker result")
async def get_noob_result(session_id: str, auth: AuthInfo = Depends(get_auth_info)) -> NoobTaskResult:
    try:
        session = _get_owned_noob_session_like(session_id, auth.user_id)
        return await task_session.get_noob_task_result(session.id)
    except (SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_session_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}/noob/events", summary="Get NOOB worker events")
async def get_noob_events(
    session_id: str,
    tail: int = 200,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskEvents:
    try:
        session = _get_owned_noob_session_like(session_id, auth.user_id)
        return await task_session.get_noob_task_events(session.id, tail=tail)
    except (SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_session_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
