from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from agoffice_domain.errors import (
    MissionAccessDeniedError,
    MissionConflictError,
    MissionNotFoundError,
    SessionAccessDeniedError,
    SessionNotFoundError,
)
from agoffice_domain.schema import MissionCreateRequest, MissionInfo, MissionListInfo
from agoffice_domain.session_mapping import SessionModel


class MissionRecord(Protocol):
    id: str
    title: str
    repo_url: str
    instruction: str
    session_id: str | None
    user_id: str
    project_id: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class MissionRepository(Protocol):
    def new_mission(self, user_id: str, request: MissionCreateRequest) -> MissionRecord: ...
    def get_mission(self, mission_id: str) -> MissionRecord | None: ...
    def list_missions(self, user_id: str, project_id: str) -> Sequence[MissionRecord]: ...
    def update_mission(
        self,
        mission_id: str,
        *,
        session_id: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> MissionRecord: ...
    def get_session(self, session_id: str) -> SessionModel | None: ...


class MissionRuntime(Protocol):
    async def start_mission(self, *, session_id: str, mission: MissionRecord) -> MissionRecord: ...


def _to_mission_info(model: MissionRecord) -> MissionInfo:
    return MissionInfo(
        id=model.id,
        title=model.title,
        repo_url=model.repo_url,
        instruction=model.instruction,
        session_id=model.session_id,
        user_id=model.user_id,
        project_id=model.project_id,
        created_at=model.created_at,
        started_at=model.started_at,
        completed_at=model.completed_at,
    )


def _get_owned_mission(repository: MissionRepository, *, mission_id: str, user_id: str) -> MissionRecord:
    mission = repository.get_mission(mission_id)
    if mission is None:
        raise MissionNotFoundError(f"Mission {mission_id} not found")
    if mission.user_id != user_id:
        raise MissionAccessDeniedError(f"Mission {mission_id} access denied")
    return mission


def _get_owned_session(repository: MissionRepository, *, session_id: str, user_id: str) -> SessionModel:
    session = repository.get_session(session_id)
    if session is None:
        raise SessionNotFoundError(f"Session {session_id} not found")
    if session.user_id != user_id:
        raise SessionAccessDeniedError(f"Session {session_id} access denied")
    return session


def create_mission(
    repository: MissionRepository,
    *,
    user_id: str,
    request: MissionCreateRequest,
) -> MissionInfo:
    return _to_mission_info(repository.new_mission(user_id=user_id, request=request))


def get_mission(
    repository: MissionRepository,
    *,
    mission_id: str,
    user_id: str,
) -> MissionInfo:
    return _to_mission_info(_get_owned_mission(repository, mission_id=mission_id, user_id=user_id))


def list_missions(
    repository: MissionRepository,
    *,
    user_id: str,
    project_id: str,
) -> MissionListInfo:
    return MissionListInfo(
        missions=[_to_mission_info(mission) for mission in repository.list_missions(user_id, project_id)]
    )


def complete_mission(
    repository: MissionRepository,
    *,
    mission_id: str,
    user_id: str,
) -> MissionInfo:
    mission = _get_owned_mission(repository, mission_id=mission_id, user_id=user_id)
    if mission.completed_at is not None:
        raise MissionConflictError(f"Mission {mission_id} is already completed")
    updated = repository.update_mission(
        mission.id,
        completed_at=datetime.now(),
    )
    return _to_mission_info(updated)


async def start_mission(
    repository: MissionRepository,
    runtime: MissionRuntime,
    *,
    mission_id: str,
    session_id: str,
    user_id: str,
) -> MissionInfo:
    mission = _get_owned_mission(repository, mission_id=mission_id, user_id=user_id)
    if session_id:
        session = _get_owned_session(repository, session_id=session_id, user_id=user_id)
        if mission.project_id != session.project_id:
            raise MissionConflictError("Mission project_id does not match session project_id")
    if mission.started_at is not None or mission.session_id is not None:
        raise MissionConflictError(f"Mission {mission_id} is already started")
    mission = await runtime.start_mission(session_id=session_id, mission=mission)
    updated = repository.update_mission(
        mission.id,
        session_id=mission.session_id,
        started_at=datetime.now(),
    )
    return _to_mission_info(updated)
