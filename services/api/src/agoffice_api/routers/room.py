from fastapi import APIRouter, Depends, HTTPException
from agpy.auth import get_auth_info, AuthInfo
import httpx
from sse_starlette.sse import EventSourceResponse

from agoffice_domain import noob_room_service, room_service
from agoffice_domain.errors import (
    NoobRoomConflictError,
    NoobThreadNotFoundError,
    RoomAccessDeniedError,
    RoomNotFoundError,
)
from agoffice_domain.schema import (
    NoobRoomCreateRequest,
    NoobRoomInfo,
    NoobTaskAcceptedResponse,
    NoobTaskEvents,
    NoobTaskResult,
    NoobTaskStatus,
    NoobThreadCreateRequest,
    NoobThreadInfo,
    NoobThreadRequest,
    NoobWorkspacePrepAcceptedResponse,
    NoobWorkspacePrepRequest,
    NoobWorkspacePrepStatus,
    RoomConfig,
    RoomInfo,
    RoomListInfo,
    RoomUpdate,
    TunnelInfo,
)
from agoffice_infra.db import database as db
from agoffice_infra.orchestration import room_k8s as task_room
from agoffice_infra.pubsub import redis as redis_service

router = APIRouter()


def _raise_http_room_error(exc: Exception) -> None:
    if isinstance(exc, RoomNotFoundError):
        raise HTTPException(status_code=404, detail="Room not found")
    if isinstance(exc, RoomAccessDeniedError):
        raise HTTPException(status_code=403, detail="Room access denied")
    if isinstance(exc, NoobThreadNotFoundError):
        raise HTTPException(status_code=404, detail="NOOB thread not found")
    if isinstance(exc, NoobRoomConflictError):
        raise HTTPException(status_code=409, detail=str(exc))
    raise exc


@router.post("/new", summary="New task room")
async def new_room(room: RoomConfig,  auth: AuthInfo = Depends(get_auth_info)) -> RoomInfo:
    return room_service.create_room(
        db,
        user_id=auth.user_id,
        room_config=room,
    )


@router.post("/noob/room/new", summary="Create a NOOB worker room")
async def new_noob_room(
    request: NoobRoomCreateRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobRoomInfo:
    try:
        return noob_room_service.create_noob_room(
            db,
            user_id=auth.user_id,
            request=request,
        )
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)


@router.post("/noob/room/{room_id}/open", summary="Open a NOOB worker room")
async def open_noob_room(room_id: str, auth: AuthInfo = Depends(get_auth_info)) -> NoobRoomInfo:
    try:
        room = noob_room_service.get_owned_noob_room(
            db,
            room_id=room_id,
            user_id=auth.user_id,
        )
        workspace_status = await task_room.get_noob_workspace_status(room.id)
        if workspace_status.status != "ready":
            raise HTTPException(status_code=409, detail=f"NOOB workspace is not ready for room {room.id}")
        await task_room.run_noob_room(
            room_id=room.id,
            user_id=auth.user_id,
            token=auth.token,
        )
        return NoobRoomInfo(
            id=room.id,
            user_id=room.user_id,
            project_id=room.project_id,
            title=room.title,
            initial_instruction=room.initial_instruction,
            created_at=room.created_at,
            updated_at=room.updated_at,
            finished_at=room.finished_at,
            config=room.config,
        )
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)


@router.post("/noob/room/{room_id}/workspace/prepare", summary="Start NOOB workspace preparation")
async def prepare_noob_workspace(
    room_id: str,
    request: NoobWorkspacePrepRequest | None = None,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobWorkspacePrepAcceptedResponse:
    try:
        prep_request = noob_room_service.resolve_prep_request(
            db,
            noob_room_id=room_id,
            user_id=auth.user_id,
            request=request,
        )
        status = await task_room.run_noob_workspace_prep(
            room_id=room_id,
            user_id=auth.user_id,
            prep_request=prep_request,
        )
        return NoobWorkspacePrepAcceptedResponse(
            status=status.status,
            job_name=task_room.get_noob_prep_job_name(room_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/noob/room/{room_id}/thread", summary="Create or reuse the active NOOB chat thread")
async def create_noob_thread(
    room_id: str,
    request: NoobThreadCreateRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobThreadInfo:
    try:
        return noob_room_service.create_or_get_thread(
            db,
            noob_room_id=room_id,
            user_id=auth.user_id,
            request=request,
        )
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)


@router.post("/noob/room/{room_id}/thread/{thread_id}/request", summary="Submit a NOOB request scoped to a chat thread")
async def submit_noob_thread_request(
    room_id: str,
    thread_id: str,
    request: NoobThreadRequest,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskAcceptedResponse:
    try:
        thread = noob_room_service.get_owned_noob_thread(
            db,
            noob_room_id=room_id,
            thread_id=thread_id,
            user_id=auth.user_id,
        )
        task_request = noob_room_service.build_thread_task_request(thread, request)
        await task_room.submit_noob_task(
            room_id=room_id,
            user_id=auth.user_id,
            token=auth.token,
            request=task_request,
        )
        db.update_noob_thread_status(thread_id, "running")
        return NoobTaskAcceptedResponse(status="accepted")
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/noob/room/{room_id}/thread/{thread_id}/status", summary="Get NOOB worker status for a chat thread")
async def get_noob_thread_status(
    room_id: str,
    thread_id: str,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskStatus:
    try:
        noob_room_service.get_owned_noob_thread(
            db,
            noob_room_id=room_id,
            thread_id=thread_id,
            user_id=auth.user_id,
        )
        status = await task_room.get_noob_task_status(room_id)
        db.update_noob_thread_status(thread_id, status.status)
        return status
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/noob/room/{room_id}/thread/{thread_id}/result", summary="Get NOOB worker result for a chat thread")
async def get_noob_thread_result(
    room_id: str,
    thread_id: str,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskResult:
    try:
        noob_room_service.get_owned_noob_thread(
            db,
            noob_room_id=room_id,
            thread_id=thread_id,
            user_id=auth.user_id,
        )
        result = await task_room.get_noob_task_result(room_id)
        if result.exit_code == 0:
            db.update_noob_thread_status(thread_id, "succeeded")
        elif result.exit_code is not None or result.error:
            db.update_noob_thread_status(thread_id, "failed")
        return result
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/noob/room/{room_id}/thread/{thread_id}/events", summary="Get NOOB worker events for a chat thread")
async def get_noob_thread_events(
    room_id: str,
    thread_id: str,
    tail: int = 200,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobTaskEvents:
    try:
        noob_room_service.get_owned_noob_thread(
            db,
            noob_room_id=room_id,
            thread_id=thread_id,
            user_id=auth.user_id,
        )
        return await task_room.get_noob_task_events(room_id, tail=tail)
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/noob/room/{room_id}/workspace/status", summary="Get NOOB workspace preparation status")
async def get_noob_workspace_status(
    room_id: str,
    auth: AuthInfo = Depends(get_auth_info),
) -> NoobWorkspacePrepStatus:
    try:
        noob_room_service.get_owned_noob_room(
            db,
            room_id=room_id,
            user_id=auth.user_id,
        )
        return await task_room.get_noob_workspace_status(room_id)
    except (RoomNotFoundError, RoomAccessDeniedError, NoobRoomConflictError, NoobThreadNotFoundError) as exc:
        _raise_http_room_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.post("/open", summary="Open task room.")
async def open_room(room_id: str,  auth: AuthInfo = Depends(get_auth_info)) -> RoomInfo:
    try:
        return await room_service.open_room(
            db,
            task_room,
            room_id=room_id,
            user_id=auth.user_id,
            token=auth.token,
        )
    except (RoomNotFoundError, RoomAccessDeniedError) as exc:
        _raise_http_room_error(exc)


@router.get("/list", summary="Task room list")
async def task_list(project_id: str, auth: AuthInfo = Depends(get_auth_info)) -> RoomListInfo:
    return room_service.list_rooms(
        db,
        user_id=auth.user_id,
        project_id=project_id,
    )


@router.post("/hook/{room_id}", summary="Webhook to receive room updates from workers")
async def hook_on_update(room_id: str, updates: RoomUpdate) -> RoomInfo:
    return await room_service.apply_room_update(
        db,
        redis_service,
        room_id=room_id,
        updates=updates,
    )


@router.get("/stream/{room_id}", summary="SSE stream for real-time room updates")
async def stream_room(room_id: str, auth: AuthInfo = Depends(get_auth_info)):
    async def event_generator():
        try:
            async for message in room_service.subscribe_room_updates(
                db,
                redis_service,
                room_id=room_id,
                user_id=auth.user_id,
            ):
                yield {"data": message}
        except (RoomNotFoundError, RoomAccessDeniedError) as exc:
            _raise_http_room_error(exc)

    return EventSourceResponse(event_generator())


@router.post("/{room_id}/tunnel/start", summary="Start VS Code tunnel for a room")
async def start_room_tunnel(room_id: str, auth: AuthInfo = Depends(get_auth_info)) -> TunnelInfo:
    try:
        return await room_service.start_room_tunnel(
            db,
            task_room,
            room_id=room_id,
            user_id=auth.user_id,
            token=auth.token,
        )
    except (RoomNotFoundError, RoomAccessDeniedError) as exc:
        _raise_http_room_error(exc)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Tunnel worker request timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Tunnel worker request failed: {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Tunnel worker is unreachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
