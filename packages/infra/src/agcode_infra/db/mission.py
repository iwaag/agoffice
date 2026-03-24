from datetime import datetime

from sqlmodel import Session, select

from agcode_domain.schema import MissionCreateRequest
from agcode_infra.db.database import get_engine, get_session
from agcode_infra.db.models import Mission


def new_mission(user_id: str, request: MissionCreateRequest) -> Mission:
    mission = Mission(
        title=request.title,
        repo_url=request.repo_url,
        instruction=request.instruction,
        user_id=user_id,
        project_id=request.project_id,
        created_at=datetime.now(),
    )
    with Session(get_engine()) as session:
        session.add(mission)
        session.flush()
        session.commit()
        session.refresh(mission)
        return mission


def get_mission(mission_id: str) -> Mission | None:
    with Session(get_engine()) as session:
        return session.get(Mission, mission_id)


def list_missions(user_id: str, project_id: str) -> list[Mission]:
    with Session(get_engine()) as session:
        stmt = (
            select(Mission)
            .where(Mission.user_id == user_id, Mission.project_id == project_id)
            .order_by(Mission.created_at.desc())
        )
        return list(session.exec(stmt).all())


def update_mission(
    mission_id: str,
    *,
    session_id: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> Mission:
    with Session(get_engine()) as session:
        mission = session.get(Mission, mission_id)
        if not mission:
            raise ValueError(f"Mission {mission_id} not found")
        if session_id is not None:
            mission.session_id = session_id
        if started_at is not None:
            mission.started_at = started_at
        if completed_at is not None:
            mission.completed_at = completed_at
        session.add(mission)
        session.commit()
        session.refresh(mission)
        return mission
