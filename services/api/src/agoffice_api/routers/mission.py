from fastapi import APIRouter, Depends, HTTPException
from agpyutils.auth import AuthInfo, get_auth_info

from agoffice_domain import mission_service
from agoffice_domain.errors import (
    MissionAccessDeniedError,
    MissionConflictError,
    MissionNotFoundError,
    SessionAccessDeniedError,
    SessionNotFoundError,
)
from agoffice_domain.schema import MissionCreateRequest, MissionInfo, MissionListInfo, MissionStartRequest
from agoffice_infra.db import mission as mission_db
from agoffice_infra.orchestration import session_k8s as task_session

router = APIRouter()


def _raise_http_mission_error(exc: Exception) -> None:
    if isinstance(exc, (MissionNotFoundError, SessionNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (MissionAccessDeniedError, SessionAccessDeniedError)):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, MissionConflictError):
        raise HTTPException(status_code=409, detail=str(exc))
    raise exc


@router.post("/new", summary="Create mission")
async def create_mission(request: MissionCreateRequest, auth: AuthInfo = Depends(get_auth_info)) -> MissionInfo:
    return mission_service.create_mission(
        mission_db,
        user_id=auth.user_id,
        request=request,
    )


@router.post("/start", summary="Start mission in a PRO session")
async def start_mission(request: MissionStartRequest, auth: AuthInfo = Depends(get_auth_info)) -> MissionInfo:
    try:
        return await mission_service.start_mission(
            mission_db,
            task_session,
            mission_id=request.mission_id,
            session_id=request.session_id,
            user_id=auth.user_id,
        )
    except (MissionNotFoundError, MissionAccessDeniedError, MissionConflictError, SessionNotFoundError, SessionAccessDeniedError) as exc:
        _raise_http_mission_error(exc)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/get", summary="Get mission")
async def get_mission(mission_id: str, auth: AuthInfo = Depends(get_auth_info)) -> MissionInfo:
    try:
        return mission_service.get_mission(
            mission_db,
            mission_id=mission_id,
            user_id=auth.user_id,
        )
    except (MissionNotFoundError, MissionAccessDeniedError) as exc:
        _raise_http_mission_error(exc)


@router.post("/complete", summary="Complete mission")
async def complete_mission(mission_id: str, auth: AuthInfo = Depends(get_auth_info)) -> MissionInfo:
    try:
        return mission_service.complete_mission(
            mission_db,
            mission_id=mission_id,
            user_id=auth.user_id,
        )
    except (MissionNotFoundError, MissionAccessDeniedError, MissionConflictError) as exc:
        _raise_http_mission_error(exc)


@router.get("/list", summary="List missions")
async def list_missions(project_id: str, auth: AuthInfo = Depends(get_auth_info)) -> MissionListInfo:
    return mission_service.list_missions(
        mission_db,
        user_id=auth.user_id,
        project_id=project_id,
    )
